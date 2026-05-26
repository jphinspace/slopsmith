"""Minigames framework — leaderboard + profile backend.

State lives under `<config_dir>/minigames/`:
  - `runs.db`      SQLite, one row per run, indexed by game_id + created_at.
  - `profile.json` Cross-minigame profile (xp, level, unlocks, totals).

Endpoints (all under /api/plugins/minigames/):
  POST /runs            submit a finished run; awards XP, evaluates unlocks
  GET  /runs            list runs (filter by game_id, scope, limit)
  GET  /profile         current XP/level/unlocks/totals
  POST /profile/reset   wipe profile + runs
  GET  /registry        list of installed minigame plugins (server-side mirror
                        of what the frontend can also see)
"""

import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field


_lock = threading.Lock()
_state = {
    "db_path": None,
    "profile_path": None,
    "plugins_dir_resolver": None,
    "log": logging.getLogger("slopsmith.plugin.minigames"),
}

# TTL cache for the minigame plugin scan.  Walking the filesystem on every
# /registry call and run submission is cheap for small plugin counts but adds
# up on repeated calls. Cache results for _REGISTRY_TTL_S seconds; callers
# that need a fresh scan (e.g. after a plugin hot-reload) can use
# _list_minigame_plugins(force_refresh=True).
_REGISTRY_TTL_S = 10
_registry_cache: dict = {"ts": 0.0, "data": []}


# ── XP / level math ───────────────────────────────────────────────────────────

def xp_for_run(score: int) -> int:
    """Default XP formula: floor(sqrt(score) * 10). Override per-game in
    the minigame manifest via `xp_formula` (not implemented in v1)."""
    if score <= 0:
        return 0
    return int(math.floor(math.sqrt(score) * 10))


def level_for_xp(xp: int) -> int:
    """Level grows with sqrt(xp): L1 at 0, L2 at 100, L3 at 400, L4 at 900..."""
    if xp <= 0:
        return 1
    return int(math.floor(math.sqrt(xp / 100))) + 1


def xp_to_next_level(xp: int) -> int:
    next_lvl = level_for_xp(xp) + 1
    threshold = (next_lvl - 1) ** 2 * 100
    return max(0, threshold - xp)


# ── Persistence helpers ───────────────────────────────────────────────────────

def _get_conn():
    db_path = _state["db_path"]
    if not db_path:
        raise RuntimeError("minigames plugin not initialised")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id       TEXT    NOT NULL,
                score         INTEGER NOT NULL,
                duration_ms   INTEGER NOT NULL DEFAULT 0,
                modifiers     TEXT    NOT NULL DEFAULT '{}',
                meta          TEXT    NOT NULL DEFAULT '{}',
                xp_awarded    INTEGER NOT NULL DEFAULT 0,
                created_at    INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS runs_game_idx ON runs(game_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS runs_score_idx ON runs(game_id, score DESC)")
        conn.commit()
    finally:
        conn.close()


def _load_profile() -> dict:
    path = _state["profile_path"]
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            _state["log"].warning(
                "profile.json contained %s instead of an object; recreating",
                type(data).__name__,
            )
        except Exception as e:
            _state["log"].warning("profile.json unreadable, recreating: %s", e)
    return {
        "xp": 0,
        "level": 1,
        "unlocks": [],
        "totals": {"runs": 0, "score": 0, "per_game": {}},
        "created_at": int(time.time()),
    }


def _save_profile(profile: dict) -> None:
    path = _state["profile_path"]
    if not path:
        raise RuntimeError("minigames plugin not initialised")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file + rename. Per Principle VII (versioned settings),
    # safety-critical writes go through temp+rename so a crash mid-write does
    # not leave a partial profile.
    fd, tmp_name = tempfile.mkstemp(prefix=".profile-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _evaluate_unlocks(profile: dict, manifest_unlocks_by_game: dict) -> list:
    """Given current XP and per-game unlock definitions, return the new list
    of unlocked IDs (game-scoped to avoid collision: 'game_id:unlock_id')."""
    raw_unlocks = profile.get("unlocks")
    # Coerce to list and filter to strings only — non-string items (e.g. ints
    # from a manual edit) would cause sorted() to fail on mixed-type comparison
    # in Python 3, and are invalid unlock IDs anyway.
    earned = set(
        v for v in (raw_unlocks if isinstance(raw_unlocks, list) else [])
        if isinstance(v, str)
    )
    # Coerce xp to numeric; non-numeric values (e.g. "100" from a manual edit)
    # would raise TypeError in the comparison `if xp >= unlock_xp` below.
    try:
        xp = float(profile.get("xp", 0))
    except (TypeError, ValueError):
        xp = 0
    for game_id, unlocks in manifest_unlocks_by_game.items():
        for u in unlocks or []:
            if not isinstance(u, dict):
                _state["log"].warning(
                    "minigame %s has a non-object unlock entry (%r); skipping",
                    game_id, type(u).__name__,
                )
                continue
            unlock_id = u.get("id")
            if not unlock_id:
                _state["log"].warning("minigame %s has an unlock entry missing 'id'; skipping", game_id)
                continue
            key = f"{game_id}:{unlock_id}"
            try:
                unlock_xp = float(u.get("xp", 0))
            except (TypeError, ValueError):
                _state["log"].warning(
                    "minigame %s has an unlock entry with non-numeric xp threshold; skipping",
                    game_id,
                )
                continue
            if xp >= unlock_xp:
                earned.add(key)
    return sorted(earned)


# ── Minigame discovery (server-side) ──────────────────────────────────────────

def _list_minigame_plugins(force_refresh: bool = False) -> list:
    """Walk the plugin directories, return the `minigame` block of every
    plugin whose plugin.json declares one. Tolerates missing/invalid JSON.

    Results are cached for _REGISTRY_TTL_S seconds to avoid rescanning the
    filesystem on every run submission and /registry request. Pass
    force_refresh=True to bypass the cache (e.g. after a hot-reload).
    """
    now = time.monotonic()
    if not force_refresh and (now - _registry_cache["ts"]) < _REGISTRY_TTL_S:
        return list(_registry_cache["data"])

    resolver = _state["plugins_dir_resolver"]
    if not resolver:
        return []
    out = []
    for pdir in resolver():
        manifest_path = pdir / "plugin.json"
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        spec = data.get("minigame")
        if not isinstance(spec, dict):
            continue
        plugin_id = data.get("id")
        if not isinstance(plugin_id, str) or not plugin_id:
            _state["log"].warning(
                "minigame plugin at %s has no valid string 'id' in plugin.json; skipping",
                pdir,
            )
            continue
        # Spread spec first so authoritative top-level fields (plugin_id,
        # version) win if the minigame block contains conflicting keys.
        out.append({
            **spec,
            "plugin_id": plugin_id,
            "version":   data.get("version"),
        })
    _registry_cache["ts"]   = time.monotonic()
    _registry_cache["data"] = out
    return list(out)


# ── Request models ────────────────────────────────────────────────────────────

class RunSubmission(BaseModel):
    game_id:     str
    score:       int   = Field(ge=0)
    duration_ms: int   = Field(ge=0, default=0)
    modifiers:   dict  = Field(default_factory=dict)
    meta:        dict  = Field(default_factory=dict)


# ── FastAPI wiring ────────────────────────────────────────────────────────────

def setup(app, context):
    config_dir = context["config_dir"]
    base = Path(config_dir) / "minigames"
    base.mkdir(parents=True, exist_ok=True)

    _state["db_path"]      = str(base / "runs.db")
    _state["profile_path"] = base / "profile.json"
    _state["log"]          = context.get("log") or _state["log"]

    # The plugin loader doesn't currently expose a list-other-plugins helper,
    # so derive the plugin directories from environment + conventions:
    #   1. SLOPSMITH_PLUGINS_DIR env var (explicit override)
    #   2. The directory that contains this plugin (plugin_self.parent) —
    #      covers the common case where all plugins live in one flat dir.
    #   3. plugin_self.parent.parent / "plugins" — covers the layout where
    #      the repo root is one level above the plugins directory.
    # Duplicates are removed via a seen-set keyed on resolved paths.
    def _resolve_plugin_dirs():
        roots = []
        env_dir = os.environ.get("SLOPSMITH_PLUGINS_DIR")
        if env_dir:
            roots.append(Path(env_dir))
        # Built-in plugins/ next to server.py (one level above this file's
        # parent when installed as a sibling).
        plugin_self = Path(__file__).resolve().parent
        for cand in (plugin_self.parent, plugin_self.parent.parent / "plugins"):
            if cand.exists() and cand.is_dir():
                roots.append(cand)
        seen = set()
        out = []
        for root in roots:
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                key = child.resolve()
                if key in seen:
                    continue
                seen.add(key)
                out.append(child)
        return out

    _state["plugins_dir_resolver"] = _resolve_plugin_dirs
    _init_db()

    log = _state["log"]
    log.info("minigames backend ready: db=%s profile=%s",
             _state["db_path"], _state["profile_path"])

    @app.post("/api/plugins/minigames/runs")
    def submit_run(submission: RunSubmission):
        # Whitelist game_id against installed minigames. This is a soft
        # check — an uninstalled minigame can still submit if its plugin
        # was loaded earlier in the session — but it catches typos.
        installed = {p["plugin_id"]: p for p in _list_minigame_plugins()}
        if submission.game_id not in installed:
            log.warning("run submitted for unknown game_id=%s; accepting anyway",
                        submission.game_id)

        xp_gained = xp_for_run(submission.score)
        created_at = int(time.time())

        with _lock:
            conn = _get_conn()
            try:
                conn.execute(
                    """INSERT INTO runs (game_id, score, duration_ms,
                                         modifiers, meta, xp_awarded, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        submission.game_id,
                        submission.score,
                        submission.duration_ms,
                        json.dumps(submission.modifiers, separators=(",", ":")),
                        json.dumps(submission.meta,      separators=(",", ":")),
                        xp_gained,
                        created_at,
                    ),
                )
                conn.commit()
                run_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            finally:
                conn.close()

            profile = _load_profile()
            # Coerce types with fallbacks so manual edits or import/export with
            # wrong types (e.g. "xp": "100" or "xp": null) don't raise here.
            def _int(val, default=0):
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return default

            profile["xp"] = max(0, _int(profile.get("xp"), 0) + xp_gained)
            profile["level"] = level_for_xp(profile["xp"])

            # Validate that totals / per_game / per-game entry are dicts; reset
            # to defaults when a manual edit or import left wrong types.
            existing_totals = profile.get("totals")
            if not isinstance(existing_totals, dict):
                profile["totals"] = {"runs": 0, "score": 0, "per_game": {}}
            totals = profile["totals"]
            totals["runs"]  = _int(totals.get("runs"), 0) + 1
            totals["score"] = _int(totals.get("score"), 0) + submission.score
            existing_per_game = totals.get("per_game")
            if not isinstance(existing_per_game, dict):
                totals["per_game"] = {}
            per_game = totals["per_game"]
            existing_g = per_game.get(submission.game_id)
            if not isinstance(existing_g, dict):
                per_game[submission.game_id] = {"runs": 0, "best_score": 0, "total_score": 0}
            g = per_game[submission.game_id]
            g["runs"]        = _int(g.get("runs"), 0) + 1
            g["total_score"] = _int(g.get("total_score"), 0) + submission.score
            g["best_score"]  = max(_int(g.get("best_score"), 0), submission.score)

            manifest_unlocks = {
                p["plugin_id"]: p.get("unlocks", []) for p in installed.values()
            }
            profile["unlocks"] = _evaluate_unlocks(profile, manifest_unlocks)
            try:
                _save_profile(profile)
            except Exception:
                # Profile save failed (e.g. disk full). Roll back the run
                # insert so the client can retry without double-awarding XP.
                try:
                    conn2 = _get_conn()
                    try:
                        conn2.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                        conn2.commit()
                    finally:
                        conn2.close()
                except Exception as del_err:
                    _state["log"].error(
                        "failed to roll back run %s after profile-save failure: %s",
                        run_id, del_err,
                    )
                raise

        return {
            "ok": True,
            "run_id": run_id,
            "xp_gained": xp_gained,
            "profile": {
                "xp": profile["xp"],
                "level": profile["level"],
                "xp_to_next_level": xp_to_next_level(profile["xp"]),
                "unlocks": profile["unlocks"],
            },
        }

    @app.get("/api/plugins/minigames/runs")
    def list_runs(game_id: str = "", scope: str = "self", limit: int = 50):
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit out of range")
        # `scope` is reserved for future cross-user comparisons. v1 is
        # single-user so 'self' and 'global' both return this install's runs.
        if scope not in ("self", "global"):
            raise HTTPException(status_code=400, detail="scope must be 'self' or 'global'")
        q = "SELECT id, game_id, score, duration_ms, modifiers, meta, xp_awarded, created_at FROM runs"
        params: list = []
        if game_id:
            q += " WHERE game_id = ?"
            params.append(game_id)
        q += " ORDER BY score DESC, created_at DESC LIMIT ?"
        params.append(limit)

        conn = _get_conn()
        try:
            rows = conn.execute(q, params).fetchall()
        finally:
            conn.close()

        def _safe_loads(raw, default=None):
            try:
                return json.loads(raw or "{}")
            except (ValueError, TypeError):
                return default if default is not None else {}

        return {
            "runs": [
                {
                    "id":          r["id"],
                    "game_id":     r["game_id"],
                    "score":       r["score"],
                    "duration_ms": r["duration_ms"],
                    "modifiers":   _safe_loads(r["modifiers"]),
                    "meta":        _safe_loads(r["meta"]),
                    "xp_awarded":  r["xp_awarded"],
                    "created_at":  r["created_at"],
                }
                for r in rows
            ]
        }

    @app.get("/api/plugins/minigames/profile")
    def get_profile():
        profile = _load_profile()
        # Coerce xp and level to ints so xp_to_next_level / level_for_xp don't
        # crash on non-integer values that can appear after a manual edit or
        # import.  Recompute level from coerced xp so the two are always in sync
        # (stale or wrong-type level in the file is ignored).
        try:
            xp = max(0, int(profile.get("xp", 0)))
        except (TypeError, ValueError):
            xp = 0
        level = level_for_xp(xp)
        return {
            **profile,
            "xp": xp,
            "level": level,
            "xp_to_next_level": xp_to_next_level(xp),
        }

    @app.post("/api/plugins/minigames/profile/reset")
    def reset_profile():
        with _lock:
            fresh = {
                "xp": 0,
                "level": 1,
                "unlocks": [],
                "totals": {"runs": 0, "score": 0, "per_game": {}},
                "created_at": int(time.time()),
            }
            path = _state["profile_path"]
            if not path:
                raise RuntimeError("minigames plugin not initialised")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Two-phase commit: stage the fresh profile to a temp file first,
            # then wipe the DB, then rename the temp into place.
            # Phase 1 failure → nothing changed (consistent).
            # Phase 2 (DB delete) failure → temp file cleaned up, nothing
            #   changed (consistent).
            # Phase 3 (rename) failure → DB wiped but old profile survives;
            #   runs are gone but profile still holds stale totals. This is
            #   acceptable because os.replace is virtually atomic on POSIX and
            #   the rename failure case requires a full filesystem error.
            fd, tmp_name = tempfile.mkstemp(prefix=".profile-reset-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(fresh, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                # Phase 2: wipe run history.
                conn = _get_conn()
                try:
                    conn.execute("DELETE FROM runs")
                    conn.commit()
                finally:
                    conn.close()
                # Phase 3: atomically install the fresh profile.
                os.replace(tmp_name, path)
                tmp_name = None  # consumed
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        return {"ok": True}

    @app.get("/api/plugins/minigames/registry")
    def registry():
        return {"minigames": _list_minigame_plugins()}
