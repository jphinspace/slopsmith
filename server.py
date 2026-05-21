"""Rocksmith Web — FastAPI backend serving highway viewer + library."""

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import shutil
from pathlib import Path

from logging_setup import configure_logging
configure_logging()

log = logging.getLogger("slopsmith.server")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from psarc import unpack_psarc, read_psarc_entries
from song import (
    anchor_to_wire,
    arrangement_string_count,
    chord_template_to_wire,
    chord_to_wire,
    hand_shape_to_wire,
    load_song,
    note_to_wire,
    phrase_to_wire,
)
from audio import find_wem_files, convert_wem
from tunings import tuning_name
import sloppak as sloppak_mod
import drums as drums_mod
import loosefolder as loosefolder_mod

import concurrent.futures
import contextvars
import inspect
import ipaddress
import re
import sqlite3
import threading
import time
import uuid
import warnings
import xml.etree.ElementTree as ET

import structlog
from fastapi import Request

app = FastAPI(title="Rocksmith Web")

# Plugins that maintain session stores can register a cleanup callback here.
# The demo-mode janitor calls every registered hook once per hour so stale
# sessions are swept without the core needing to know plugin internals.
_DEMO_JANITOR_HOOKS: list = []
_DEMO_JANITOR_HOOKS_LOCK = threading.Lock()
_DEMO_JANITOR_STARTED = False
_DEMO_JANITOR_STOP = threading.Event()
_DEMO_JANITOR_THREAD: threading.Thread | None = None


def register_demo_janitor_hook(fn) -> None:
    """Register a zero-argument callable to be invoked hourly by the demo
    janitor.  Plugins call this from their ``setup(app, context)`` when they
    want to participate in session cleanup under demo mode.

    The callable must accept no required arguments.  Async (coroutine)
    functions are rejected: the janitor runs in a plain thread and cannot
    await coroutines.
    """
    if not callable(fn):
        raise TypeError(
            f"register_demo_janitor_hook expects a callable, got {type(fn).__name__!r}"
        )
    # Reject coroutine functions — check both the callable itself and its
    # __call__ method so objects with an async __call__ (e.g. class instances,
    # functools.partial wrappers around async functions) are also caught.
    _call = getattr(fn, "__call__", None)
    if inspect.iscoroutinefunction(fn) or (
        _call is not None and inspect.iscoroutinefunction(_call)
    ):
        raise TypeError(
            "register_demo_janitor_hook does not accept async functions; "
            "the janitor runs in a plain thread and cannot await coroutines"
        )
    # Validate that the callable accepts zero required arguments so it won't
    # crash at sweep time (hourly, far from the registration site).
    try:
        sig = inspect.signature(fn)
    except ValueError:
        # inspect.signature() raises ValueError for built-in C callables whose
        # signature cannot be determined.  Accept them as-is; if they fail at
        # runtime the janitor will catch and log the exception.
        pass
    else:
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]
        if required:
            raise TypeError(
                f"register_demo_janitor_hook expects a zero-argument callable; "
                f"{fn!r} has {len(required)} required parameter(s): "
                + ", ".join(p.name for p in required)
            )
    with _DEMO_JANITOR_HOOKS_LOCK:
        _DEMO_JANITOR_HOOKS.append(fn)


def _run_janitor_hook(hook) -> None:
    """Run a single janitor hook inline, swallowing and logging any exception.

    If the hook returns an awaitable (e.g. a coroutine slipped through the
    async-function guard), the coroutine is closed immediately to avoid
    ``RuntimeWarning: coroutine was never awaited`` noise, and a warning is
    emitted so the plugin author knows to fix their hook.
    """
    try:
        result = hook()
    except Exception:
        log.exception("janitor hook %r raised", hook)
        return
    if inspect.iscoroutine(result):
        # A coroutine slipped through the async-function guard (e.g. via a
        # wrapper/partial).  Close it to suppress "coroutine never awaited",
        # then warn so the plugin author knows to fix their hook.
        try:
            result.close()
        except Exception:
            log.exception("error closing coroutine from janitor hook %r", hook)
        warnings.warn(
            f"janitor hook {hook!r} returned a coroutine; "
            "hooks must be plain synchronous callables — "
            "register_demo_janitor_hook does not accept async functions",
            RuntimeWarning,
            stacklevel=1,
        )
    elif inspect.isawaitable(result):
        # Future/Task: no .close() method; just warn and leave it alone.
        warnings.warn(
            f"janitor hook {hook!r} returned an awaitable (Future/Task); "
            "hooks must be plain synchronous callables",
            RuntimeWarning,
            stacklevel=1,
        )


_DEMO_BLOCKED: list[tuple[str, re.Pattern]] = [
    ("POST",   re.compile(r"^/api/settings$")),
    ("POST",   re.compile(r"^/api/settings/import$")),
    ("POST",   re.compile(r"^/api/rescan$")),
    ("POST",   re.compile(r"^/api/rescan/full$")),
    ("POST",   re.compile(r"^/api/songs/upload$")),
    ("DELETE", re.compile(r"^/api/song/.+$")),
    ("POST",   re.compile(r"^/api/favorites/toggle$")),
    ("POST",   re.compile(r"^/api/loops$")),
    ("DELETE", re.compile(r"^/api/loops/[^/]+$")),
    ("POST",   re.compile(r"^/api/song/.*/meta$")),
    ("POST",   re.compile(r"^/api/song/.*/art/upload$")),
    ("GET",    re.compile(r"^/api/plugins/updates$")),
    ("POST",   re.compile(r"^/api/plugins/[^/]+/update$")),
    ("POST",   re.compile(r"^/api/plugins/editor/save$")),
    ("POST",   re.compile(r"^/api/plugins/editor/build$")),
    ("POST",   re.compile(r"^/api/plugins/editor/upload-art$")),
    ("POST",   re.compile(r"^/api/plugins/editor/upload-audio$")),
    ("POST",   re.compile(r"^/api/plugins/editor/youtube-audio$")),
    ("POST",   re.compile(r"^/api/plugins/editor/import-gp$")),
    ("POST",   re.compile(r"^/api/plugins/editor/import-midi$")),
    ("POST",   re.compile(r"^/api/plugins/lyrics_karaoke/align$")),
    ("POST",   re.compile(r"^/api/plugins/lyrics_karaoke/generate-pitch$")),
    ("POST",   re.compile(r"^/api/plugins/lyrics_karaoke/save-lyrics$")),
    ("POST",   re.compile(r"^/api/plugins/lyrics_sync/align$")),
    ("POST",   re.compile(r"^/api/plugins/lyrics_sync/save$")),
    ("POST",   re.compile(r"^/api/plugins/studio/sessions/[^/]+/extract-drums$")),
    ("POST",   re.compile(r"^/api/diagnostics/export$")),
    ("GET",    re.compile(r"^/api/diagnostics/preview$")),
    ("GET",    re.compile(r"^/api/diagnostics/hardware$")),
    # Bundled core plugin — video background upload/delete
    ("POST",   re.compile(r"^/api/plugins/highway_3d/files$")),
    ("DELETE", re.compile(r"^/api/plugins/highway_3d/files$")),
]


@app.middleware("http")
async def _demo_mode_guard(request: Request, call_next):
    if os.environ.get("SLOPSMITH_DEMO_MODE") == "1":
        path = request.url.path
        for method, pattern in _DEMO_BLOCKED:
            if request.method == method and pattern.match(path):
                return JSONResponse({"error": "demo mode: read-only"}, status_code=403)
        response = await call_next(request)
        if request.method == "GET" and path == "/" and "slopsmith_demo_session" not in request.cookies:
            forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            is_secure = request.url.scheme == "https" or forwarded_proto.lower() == "https"
            response.set_cookie(
                "slopsmith_demo_session", str(uuid.uuid4()),
                max_age=86400, httponly=True, samesite="lax",
                secure=is_secure,
            )
        return response
    return await call_next(request)

from asgi_correlation_id import CorrelationIdMiddleware

# validator=None accepts any non-empty inbound X-Request-ID value, including
# opaque proxy-generated hex strings, not just RFC-4122 UUIDs.
app.add_middleware(CorrelationIdMiddleware, validator=None)

STATIC_DIR = Path(__file__).parent / "static"
try:
    STATIC_DIR.mkdir(exist_ok=True)
except OSError:
    pass  # Read-only in packaged installs

# Distinguish "env not set / empty" from "explicitly set". Path("") collapses
# to Path(".") so we can't recover that signal after the cast — capture the
# raw env-var string up front and let _get_dlc_dir() consult both. This way
# `DLC_DIR=.` remains a valid opt-in for cwd while `DLC_DIR=""` (or unset)
# falls through to the config.json fallback.
_DLC_DIR_ENV = os.environ.get("DLC_DIR", "").strip()
DLC_DIR = Path(_DLC_DIR_ENV) if _DLC_DIR_ENV else Path("")
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(Path.home() / ".local" / "share" / "rocksmith-cdlc")))

# Writable cache directories (use CONFIG_DIR, not STATIC_DIR which may be read-only)
ART_CACHE_DIR = CONFIG_DIR / "art_cache"
AUDIO_CACHE_DIR = CONFIG_DIR / "audio_cache"
SLOPPAK_CACHE_DIR = CONFIG_DIR / "sloppak_cache"


def _env_flag(name: str) -> bool:
    """Parse a conventional boolean env flag."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ── SQLite metadata cache ─────────────────────────────────────────────────────

class MetadataDB:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = str(CONFIG_DIR / "web_library.db")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                filename TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                title TEXT,
                artist TEXT,
                album TEXT,
                year TEXT,
                duration REAL,
                tuning TEXT,
                arrangements TEXT,
                has_lyrics INTEGER DEFAULT 0,
                format TEXT DEFAULT 'psarc',
                stem_count INTEGER DEFAULT 0,
                stem_ids TEXT DEFAULT '[]',
                tuning_name TEXT DEFAULT '',
                tuning_sort_key INTEGER DEFAULT 0
            )
        """)
        # Idempotent migrations for installs that predate each column.
        for ddl in (
            "ALTER TABLE songs ADD COLUMN format TEXT DEFAULT 'psarc'",
            "ALTER TABLE songs ADD COLUMN stem_count INTEGER DEFAULT 0",
            # slopsmith#129: per-stem filter needs the id list, not just count.
            "ALTER TABLE songs ADD COLUMN stem_ids TEXT DEFAULT '[]'",
            # slopsmith#69 + #22: denormalized canonical tuning name + numeric
            # sort key (sum of offsets). The existing `tuning` text column
            # stays — these are caches, repopulated on rescan.
            "ALTER TABLE songs ADD COLUMN tuning_name TEXT DEFAULT ''",
            "ALTER TABLE songs ADD COLUMN tuning_sort_key INTEGER DEFAULT 0",
        ):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist COLLATE NOCASE)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title COLLATE NOCASE)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_tuning_name ON songs(tuning_name COLLATE NOCASE)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_tuning_sort_key ON songs(tuning_sort_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_year ON songs(year)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS favorites (filename TEXT PRIMARY KEY)")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS loops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()
        self._lock = threading.Lock()

    def is_favorite(self, filename: str) -> bool:
        return self.conn.execute("SELECT 1 FROM favorites WHERE filename = ?", (filename,)).fetchone() is not None

    def toggle_favorite(self, filename: str) -> bool:
        """Toggle favorite status. Returns new state."""
        with self._lock:
            if self.is_favorite(filename):
                self.conn.execute("DELETE FROM favorites WHERE filename = ?", (filename,))
                self.conn.commit()
                return False
            else:
                self.conn.execute("INSERT OR IGNORE INTO favorites VALUES (?)", (filename,))
                self.conn.commit()
                return True

    def favorite_set(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT filename FROM favorites").fetchall()}

    def get(self, filename: str, mtime: float, size: int) -> dict | None:
        row = self.conn.execute(
            "SELECT mtime, size, title, artist, album, year, duration, tuning, arrangements, has_lyrics, "
            "format, stem_count, stem_ids, tuning_name, tuning_sort_key "
            "FROM songs WHERE filename = ?", (filename,)
        ).fetchone()
        if row and row[0] == mtime and row[1] == size and row[2]:
            return {
                "title": row[2], "artist": row[3], "album": row[4],
                "year": row[5], "duration": row[6], "tuning": row[7],
                "arrangements": json.loads(row[8]) if row[8] else [],
                "has_lyrics": bool(row[9]),
                "format": row[10] or "psarc",
                "stem_count": int(row[11] or 0),
                "stem_ids": json.loads(row[12]) if row[12] else [],
                "tuning_name": row[13] or "",
                "tuning_sort_key": int(row[14] or 0),
            }
        return None

    def put(self, filename: str, mtime: float, size: int, meta: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO songs "
                "(filename, mtime, size, title, artist, album, year, duration, tuning, arrangements, "
                "has_lyrics, format, stem_count, stem_ids, tuning_name, tuning_sort_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (filename, mtime, size, meta.get("title", ""), meta.get("artist", ""),
                 meta.get("album", ""), meta.get("year", ""), meta.get("duration", 0),
                 meta.get("tuning", ""), json.dumps(meta.get("arrangements", [])),
                 1 if meta.get("has_lyrics") else 0,
                 meta.get("format", "psarc"),
                 int(meta.get("stem_count", 0) or 0),
                 json.dumps(meta.get("stem_ids", []) or []),
                 meta.get("tuning_name", "") or "",
                 int(meta.get("tuning_sort_key", 0) or 0)),
            )
            self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM songs WHERE title != ''").fetchone()[0]

    def delete_missing(self, current_filenames: set[str]):
        """Remove DB entries for files no longer on disk."""
        with self._lock:
            db_files = {r[0] for r in self.conn.execute("SELECT filename FROM songs").fetchall()}
            stale = db_files - current_filenames
            if stale:
                self.conn.executemany("DELETE FROM songs WHERE filename = ?", [(f,) for f in stale])
                self.conn.commit()
            return len(stale)

    def _estd_set(self) -> set[str]:
        """Get set of filenames that have a retuned variant (_EStd_ or _DropD_) in the DB."""
        rows = self.conn.execute(
            "SELECT filename FROM songs WHERE filename LIKE '%\\_EStd\\_%' ESCAPE '\\' "
            "OR filename LIKE '%\\_DropD\\_%' ESCAPE '\\'"
        ).fetchall()
        originals = set()
        for (fname,) in rows:
            originals.add(fname.replace("_EStd_", "_").replace("_DropD_", "_"))
        return originals

    # Manifest-allowed filter values. Whitelisted before binding so a
    # malformed query string can't push arbitrary text through to SQL —
    # parameters are bound, but capping the input space is still cheap
    # defense-in-depth (see slopsmith#129).
    _ALLOWED_ARRANGEMENT_NAMES = {"Lead", "Rhythm", "Bass", "Combo"}
    # Stem ids match the bare strings sloppak manifests use today —
    # `full`, `guitar`, `bass`, `drums`, `vocals`, `piano`, `other`. The
    # frontend filter UI omits `full` (it's the always-on fallback mix
    # and would match every sloppak), but the server-side whitelist
    # keeps it so a hand-rolled API client can still ask for it.
    _ALLOWED_STEM_IDS = {"full", "guitar", "bass", "drums", "vocals", "piano", "other"}

    def _build_where(self, q: str = "", favorites_only: bool = False,
                     format_filter: str = "",
                     arrangements_has: list[str] | None = None,
                     arrangements_lacks: list[str] | None = None,
                     stems_has: list[str] | None = None,
                     stems_lacks: list[str] | None = None,
                     has_lyrics: int | None = None,
                     tunings: list[str] | None = None) -> tuple[str, list]:
        """Shared WHERE-clause builder for query_page / query_artists /
        query_stats. Returns (where_sql, params). Leading 'WHERE' is
        included so callers paste it directly. See slopsmith#129/#69.
        """
        where = "WHERE title != ''"
        params: list = []
        if favorites_only:
            where += " AND filename IN (SELECT filename FROM favorites)"
        if format_filter:
            where += " AND format = ?"
            params.append(format_filter)
        if q:
            where += " AND (title LIKE ? COLLATE NOCASE OR artist LIKE ? COLLATE NOCASE OR album LIKE ? COLLATE NOCASE)"
            params += [f"%{q}%"] * 3
        # arrangements_has: OR within axis (any-of). Uses JSON1's
        # json_each which yields one row per arrangement, then matches
        # the `name` field. The whole subquery is wrapped in EXISTS so
        # we don't multiply rows in the outer SELECT.
        arr_has = [a for a in (arrangements_has or []) if a in self._ALLOWED_ARRANGEMENT_NAMES]
        if arr_has:
            placeholders = ",".join(["?"] * len(arr_has))
            where += (" AND EXISTS (SELECT 1 FROM json_each(songs.arrangements) "
                      f"WHERE json_extract(value, '$.name') IN ({placeholders}))")
            params += arr_has
        arr_lacks = [a for a in (arrangements_lacks or []) if a in self._ALLOWED_ARRANGEMENT_NAMES]
        if arr_lacks:
            placeholders = ",".join(["?"] * len(arr_lacks))
            where += (" AND NOT EXISTS (SELECT 1 FROM json_each(songs.arrangements) "
                      f"WHERE json_extract(value, '$.name') IN ({placeholders}))")
            params += arr_lacks
        stems_h = [s for s in (stems_has or []) if s in self._ALLOWED_STEM_IDS]
        if stems_h:
            placeholders = ",".join(["?"] * len(stems_h))
            where += (" AND EXISTS (SELECT 1 FROM json_each(songs.stem_ids) "
                      f"WHERE value IN ({placeholders}))")
            params += stems_h
        stems_l = [s for s in (stems_lacks or []) if s in self._ALLOWED_STEM_IDS]
        if stems_l:
            placeholders = ",".join(["?"] * len(stems_l))
            where += (" AND NOT EXISTS (SELECT 1 FROM json_each(songs.stem_ids) "
                      f"WHERE value IN ({placeholders}))")
            params += stems_l
        if has_lyrics in (0, 1):
            where += " AND has_lyrics = ?"
            params.append(has_lyrics)
        if tunings:
            # Keep the input cap conservative (32) so a hostile caller
            # can't blow out the parameter list. Real tuning sets in the
            # wild number in the low double digits.
            tn = [t for t in tunings if isinstance(t, str) and t][:32]
            if tn:
                placeholders = ",".join(["?"] * len(tn))
                where += f" AND tuning_name COLLATE NOCASE IN ({placeholders})"
                params += tn
        return where, params

    def query_page(self, q: str = "", page: int = 0, size: int = 24,
                   sort: str = "artist", direction: str = "asc",
                   favorites_only: bool = False,
                   format_filter: str = "",
                   arrangements_has: list[str] | None = None,
                   arrangements_lacks: list[str] | None = None,
                   stems_has: list[str] | None = None,
                   stems_lacks: list[str] | None = None,
                   has_lyrics: int | None = None,
                   tunings: list[str] | None = None) -> tuple[list[dict], int]:
        """Server-side paginated search. Returns (songs, total_count)."""
        where, params = self._build_where(
            q=q, favorites_only=favorites_only, format_filter=format_filter,
            arrangements_has=arrangements_has, arrangements_lacks=arrangements_lacks,
            stems_has=stems_has, stems_lacks=stems_lacks,
            has_lyrics=has_lyrics, tunings=tunings,
        )

        sort_map = {
            "artist": "artist COLLATE NOCASE", "artist-desc": "artist COLLATE NOCASE DESC",
            "title": "title COLLATE NOCASE", "title-desc": "title COLLATE NOCASE DESC",
            "recent": "mtime DESC",
            # Tuning sort uses musical distance from E Standard
            # (slopsmith#22 — was alphabetical). `tuning_sort_key` is
            # the sum of per-string offsets, so |sort_key| is the
            # magnitude of the down/up-tune. ABS ascending puts E
            # Standard (0) first, then ±2 (Drop D, F Standard), then
            # ±6 (Eb Standard, F# Standard), and so on. Within a
            # magnitude tier we break ties by signed key ASC so the
            # negative (down-tuned) variant comes before the positive
            # (up-tuned) one — Eb Standard before F Standard, matching
            # how Rocksmith groups its tuning list. Final tiebreak by
            # name keeps the order fully deterministic.
            #
            # Leading term pushes pre-migration / unscanned rows to
            # the bottom — without it ABS(0) collides with E
            # Standard's 0 and unindexed rows would sort first.
            # COALESCE on every column the clause references guards
            # against NULL values — SQLite's literal-constant ADD
            # COLUMN does backfill on most versions, but raw SQL
            # inserts that bypass `put()`, edge-case migration paths,
            # or future code that writes None could still leave NULLs
            # behind, and a NULL `tuning_name` in `(tuning_name = '')`
            # evaluates to NULL itself (which sorts ahead of 0 in
            # ASC), defeating the push-to-bottom intent.
            "tuning": (
                "(COALESCE(tuning_name, '') = '') ASC, "
                "ABS(COALESCE(tuning_sort_key, 0)), "
                "COALESCE(tuning_sort_key, 0) ASC, "
                "COALESCE(tuning_name, '') COLLATE NOCASE"
            ),
            # Year sort (slopsmith#128). Empty-year rows pushed to the
            # bottom for both directions; otherwise CAST so '2010' >
            # '2005' rather than alphabetic.
            "year": "(year = '') ASC, CAST(year AS INTEGER) ASC",
            "year-desc": "(year = '') ASC, CAST(year AS INTEGER) DESC",
        }
        order = sort_map.get(sort, "artist COLLATE NOCASE")
        # Legacy `dir=desc` toggle: only safe to append on simple sort
        # clauses that don't already encode a direction. Compound /
        # multi-term entries above (tuning, year, year-desc) bake their
        # ASC/DESC into the clause, so a global ` DESC` append would
        # produce invalid SQL like `CAST(year AS INTEGER) ASC DESC`.
        # Skip the append in that case — clients flipping direction on
        # those sorts use the explicit `-desc` sort key instead.
        if direction == "desc" and " ASC" not in order and " DESC" not in order:
            order += " DESC"

        total = self.conn.execute(f"SELECT COUNT(*) FROM songs {where}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT filename, title, artist, album, year, duration, tuning, arrangements, has_lyrics, mtime, "
            f"format, stem_count, stem_ids, tuning_name "
            f"FROM songs {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [size, page * size]
        ).fetchall()

        estd = self._estd_set()
        favs = self.favorite_set()
        songs = []
        for r in rows:
            songs.append({
                "filename": r[0], "title": r[1], "artist": r[2], "album": r[3],
                "year": r[4], "duration": r[5], "tuning": r[6],
                "arrangements": json.loads(r[7]) if r[7] else [],
                "has_lyrics": bool(r[8]), "mtime": r[9],
                "format": r[10] or "psarc",
                "stem_count": int(r[11] or 0),
                "stem_ids": json.loads(r[12]) if r[12] else [],
                "tuning_name": r[13] or "",
                "has_estd": r[0] in estd, "favorite": r[0] in favs,
            })
        return songs, total

    def query_artists(self, letter: str = "", q: str = "",
                      favorites_only: bool = False,
                      page: int = 0, size: int = 50,
                      format_filter: str = "",
                      arrangements_has: list[str] | None = None,
                      arrangements_lacks: list[str] | None = None,
                      stems_has: list[str] | None = None,
                      stems_lacks: list[str] | None = None,
                      has_lyrics: int | None = None,
                      tunings: list[str] | None = None) -> tuple[list[dict], int]:
        """Get artists grouped by letter with their albums and songs. Returns (artists, total_artists)."""
        where, params = self._build_where(
            q=q, favorites_only=favorites_only, format_filter=format_filter,
            arrangements_has=arrangements_has, arrangements_lacks=arrangements_lacks,
            stems_has=stems_has, stems_lacks=stems_lacks,
            has_lyrics=has_lyrics, tunings=tunings,
        )
        if letter == "#":
            where += " AND artist NOT GLOB '[A-Za-z]*'"
        elif letter:
            where += " AND UPPER(SUBSTR(artist, 1, 1)) = ?"
            params.append(letter.upper())

        # Get paginated distinct artists
        total_artists = self.conn.execute(
            f"SELECT COUNT(DISTINCT artist COLLATE NOCASE) FROM songs {where}", params
        ).fetchone()[0]

        artist_rows = self.conn.execute(
            f"SELECT DISTINCT artist COLLATE NOCASE as a FROM songs {where} ORDER BY a LIMIT ? OFFSET ?",
            params + [size, page * size]
        ).fetchall()
        artist_names = [r[0] for r in artist_rows]

        if not artist_names:
            return [], total_artists

        # Fetch songs for these artists only
        placeholders = ",".join(["?"] * len(artist_names))
        song_where = f"{where} AND artist COLLATE NOCASE IN ({placeholders})"
        song_params = params + artist_names

        rows = self.conn.execute(
            f"SELECT filename, title, artist, album, year, duration, tuning, arrangements, has_lyrics, "
            f"format, stem_count, stem_ids, tuning_name "
            f"FROM songs {song_where} ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title COLLATE NOCASE",
            song_params
        ).fetchall()

        # Group into artist -> album -> songs
        from collections import OrderedDict
        estd = self._estd_set()
        favs = self.favorite_set()
        artists = OrderedDict()
        for r in rows:
            artist = r[2] or "Unknown Artist"
            album = r[3] or "Unknown Album"
            akey = artist.lower()
            if akey not in artists:
                artists[akey] = {"name": artist, "albums": OrderedDict()}
            bkey = album.lower()
            if bkey not in artists[akey]["albums"]:
                artists[akey]["albums"][bkey] = {"name": album, "songs": []}
            artists[akey]["albums"][bkey]["songs"].append({
                "filename": r[0], "title": r[1], "artist": r[2], "album": r[3],
                "year": r[4], "duration": r[5], "tuning": r[6],
                "arrangements": json.loads(r[7]) if r[7] else [],
                "has_lyrics": bool(r[8]),
                "format": r[9] or "psarc",
                "stem_count": int(r[10] or 0),
                "stem_ids": json.loads(r[11]) if r[11] else [],
                "tuning_name": r[12] or "",
                "has_estd": r[0] in estd,
                "favorite": r[0] in favs,
            })

        # Pick most common name variant per artist/album
        result = []
        for akey, aval in artists.items():
            albums = []
            for bkey, bval in aval["albums"].items():
                albums.append({"name": bval["name"], "songs": bval["songs"]})
            result.append({"name": aval["name"], "album_count": len(albums),
                           "song_count": sum(len(a["songs"]) for a in albums), "albums": albums})
        return result, total_artists

    def query_stats(self, favorites_only: bool = False,
                    q: str = "", format_filter: str = "",
                    arrangements_has: list[str] | None = None,
                    arrangements_lacks: list[str] | None = None,
                    stems_has: list[str] | None = None,
                    stems_lacks: list[str] | None = None,
                    has_lyrics: int | None = None,
                    tunings: list[str] | None = None) -> dict:
        """Aggregate stats for the letter bar. Accepts the same filter
        params as query_page so the letter counts stay synchronized
        with the grid when filters are active."""
        where, params = self._build_where(
            q=q, favorites_only=favorites_only, format_filter=format_filter,
            arrangements_has=arrangements_has, arrangements_lacks=arrangements_lacks,
            stems_has=stems_has, stems_lacks=stems_lacks,
            has_lyrics=has_lyrics, tunings=tunings,
        )
        total = self.conn.execute(f"SELECT COUNT(*) FROM songs {where}", params).fetchone()[0]
        # NOCASE collation here mirrors `query_artists` and the per-
        # letter `COUNT(DISTINCT artist COLLATE NOCASE)` below — without
        # it, an artist stored under two different casings would inflate
        # `total_artists` against the letter-bar breakdown the UI
        # renders next to it.
        artist_count = self.conn.execute(
            f"SELECT COUNT(DISTINCT artist COLLATE NOCASE) FROM songs {where}", params
        ).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT UPPER(SUBSTR(artist, 1, 1)) as letter, COUNT(DISTINCT artist COLLATE NOCASE) "
            f"FROM songs {where} GROUP BY letter", params
        ).fetchall()
        letters = {}
        for letter, count in rows:
            if letter and letter.isalpha():
                letters[letter] = count
            else:
                letters["#"] = letters.get("#", 0) + count
        return {"total_songs": total, "total_artists": artist_count, "letters": letters}


meta_db = MetadataDB()


def _get_dlc_dir(cfg: dict | None = None) -> Path | None:
    # Only consider DLC_DIR if the env var was non-empty. `Path("")` collapses
    # to `.` and reports `.is_dir() == True`, which would silently shadow the
    # config.json fallback. Checking the raw env string preserves
    # `DLC_DIR=.` as a valid opt-in for cwd while keeping unset/empty out.
    if _DLC_DIR_ENV and DLC_DIR.is_dir():
        return DLC_DIR
    if cfg is None:
        config_file = CONFIG_DIR / "config.json"
        if config_file.exists():
            try:
                cfg = json.loads(config_file.read_text())
            except Exception:
                pass
    if isinstance(cfg, dict):
        raw = str(cfg.get("dlc_dir", "")).strip()
        if raw:
            p = Path(raw)
            if p.is_dir():
                return p
    return None


# ── Background metadata scan ──────────────────────────────────────────────────

def _extract_meta_fast(psarc_path: Path) -> dict:
    """Extract metadata from a PSARC using in-memory reading (no disk I/O)."""
    files = read_psarc_entries(str(psarc_path), ["*.json", "*.xml", "*vocals*.sng"])

    title = artist = album = year = ""
    duration = 0.0
    tuning = "E Standard"
    # Track the offsets for the tuning we ultimately keep so we can
    # compute tuning_sort_key (#22) without re-deriving it from the
    # name. Defaults to E Standard offsets.
    tuning_offsets: list[int] = [0] * 6
    _tuning_from_guitar = False
    arrangements = []
    has_lyrics = False
    arr_index = 0

    # Parse manifest JSONs for metadata + arrangement info
    for path, data in sorted(files.items()):
        if not path.lower().endswith(".json"):
            continue
        try:
            jdata = json.loads(data)
            entries = jdata.get("Entries") or {}
            for k, v in entries.items():
                attrs = v.get("Attributes") or {}
                arr_name = attrs.get("ArrangementName", "")
                if arr_name in ("Vocals", "ShowLights", "JVocals"):
                    continue
                if not title:
                    title = attrs.get("SongName", "")
                    artist = attrs.get("ArtistName", "")
                    album = attrs.get("AlbumName", "")
                    yr = attrs.get("SongYear")
                    year = str(yr) if yr else ""
                    sl = attrs.get("SongLength")
                    if sl:
                        try: duration = float(sl)
                        except (ValueError, TypeError): pass
                if arr_name:
                    # Get tuning - prefer guitar arrangements over bass
                    tun = attrs.get("Tuning")
                    if tun and isinstance(tun, dict):
                        offsets = [tun.get(f"string{i}", 0) for i in range(6)]
                        tun_name = tuning_name(offsets)
                        is_guitar = arr_name in ("Lead", "Rhythm", "Combo")
                        if tuning == "E Standard" or (is_guitar and not _tuning_from_guitar):
                            tuning = tun_name
                            tuning_offsets = offsets
                            if is_guitar:
                                _tuning_from_guitar = True
                    notes = attrs.get("NotesHard", 0) or attrs.get("NotesMedium", 0) or 0
                    arrangements.append({"index": arr_index, "name": arr_name, "notes": notes})
                    arr_index += 1
        except Exception:
            continue

    # Check XMLs for vocals (CDLC), or fall back to vocals SNG (official DLC)
    for path, data in files.items():
        if path.lower().endswith(".xml"):
            try:
                root = ET.fromstring(data)
                if root.tag == "vocals":
                    has_lyrics = True
                    break
            except Exception:
                continue
        elif path.lower().endswith(".sng") and "vocals" in path.lower():
            has_lyrics = True
            break

    # Sort arrangements: Lead > Combo > Rhythm > Bass
    priority = {"Lead": 0, "Combo": 1, "Rhythm": 2, "Bass": 3}
    arrangements.sort(key=lambda a: priority.get(a["name"], 99))
    for i, a in enumerate(arrangements):
        a["index"] = i

    return {
        "title": title, "artist": artist, "album": album, "year": year,
        "duration": duration, "tuning": tuning,
        "arrangements": arrangements, "has_lyrics": has_lyrics,
        # PSARCs have no stems; emit an empty list so the column round-
        # trips uniformly with sloppaks (slopsmith#129).
        "stem_ids": [],
        # Cached tuning fields (slopsmith#22 / #69). The text `tuning`
        # column above stays the source of truth for display; these are
        # the indexable / filterable forms.
        "tuning_name": tuning,
        "tuning_sort_key": sum(tuning_offsets),
    }


def _extract_meta_sloppak(path: Path) -> dict:
    """Extract metadata for a sloppak (file or directory)."""
    meta = sloppak_mod.extract_meta(path)
    offsets = meta.pop("tuning_offsets", None) or [0] * 6
    name = tuning_name(offsets)
    meta["tuning"] = name
    meta["tuning_name"] = name
    meta["tuning_sort_key"] = sum(offsets)
    meta["format"] = "sloppak"
    # `extract_meta` already populates `stem_ids` (slopsmith#129);
    # default to empty for older callers / mocks.
    meta.setdefault("stem_ids", [])
    return meta


def _resolve_dlc_path(dlc: Path, filename: str) -> Path | None:
    """Resolve `filename` under DLC_DIR and refuse anything that escapes.

    `filename` arrives from `:path` route params and can contain `..`
    segments. The Sloppak and PSARC paths happen to fail safely later
    because their loaders raise on missing/invalid files, but loose-
    folder format detection (`is_loose_song`) globs and parses XML on
    disk first, which lets a crafted path trigger filesystem reads
    outside DLC_DIR before any guard fires. Centralise the containment
    check so every filename-bound handler validates before touching the
    filesystem.

    Returns the validated resolved Path, or None if the path is empty
    or escapes the DLC root.
    """
    if not filename:
        return None
    try:
        resolved = (dlc / filename).resolve()
        resolved.relative_to(dlc.resolve())
    except (ValueError, OSError):
        return None
    return resolved


def _sanitized_song_offset(song) -> float:
    """Return song.offset coerced to a finite float, or 0.0.

    Malformed loose-folder XMLs can put `NaN`/`Infinity` into <offset>;
    Python's `float()` happily accepts those, but Starlette's JSON
    encoder then emits the literal `NaN` token which is invalid JSON
    and breaks the frontend's song_info parsing.
    """
    try:
        v = float(getattr(song, "offset", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _stat_for_cache(f: Path) -> tuple[float, int]:
    """Return (mtime, size) for cache freshness checks.

    For loose-folder directories the directory's own mtime does not
    change when inner files (audio.wem / *.xml / manifest.json) are
    edited in place, so we aggregate over the contents. PSARCs and
    sloppak files (zip form) use their own stat directly; sloppak
    directories aren't covered here because `meta_db` already keys them
    by `.sloppak` path and they aren't subject to the same edit-in-place
    pattern.
    """
    # Aggregate inner stats for loose folders. We detect "loose-shape"
    # purely by file presence (xml + wem + optional manifest.json) so
    # this stays O(stat) on the hot path — `/api/song/{filename}` and
    # the background scan call this on every check, and we avoid
    # calling `is_loose_song` here because that would parse XML on
    # every cache lookup.
    # A sloppak directory has manifest.yaml + arrangement JSON + ogg
    # stems instead, so it won't match the loose globs and falls
    # through to the directory-stat path (same behaviour as before
    # this PR).
    if f.is_dir():
        # Skip symlinks pointing outside the song folder — without this
        # an attacker-crafted CDLC could keep a stale cache hot by
        # bumping the mtime of an unrelated file via a symlink.
        root = f.resolve()
        def _in_folder(p: Path) -> bool:
            try:
                p.resolve().relative_to(root)
            except (OSError, ValueError):
                return False
            return True
        xmls = [p for p in f.glob("*.xml") if _in_folder(p)]
        wems = [p for p in f.glob("*.wem") if _in_folder(p)]
        if xmls and wems:
            inner = xmls + wems + [p for p in f.glob("manifest.json") if _in_folder(p)]
            # Tolerate files vanishing between glob() and stat() —
            # otherwise a concurrent edit/move in DLC_DIR can let an
            # OSError bubble out of _background_scan(), killing the
            # scan thread while `_scan_status["running"]` stays true.
            stats = []
            for p in inner:
                try:
                    stats.append(p.stat())
                except OSError:
                    continue
            if stats:
                return max(s.st_mtime for s in stats), sum(s.st_size for s in stats)
    st = f.stat()
    return st.st_mtime, st.st_size


def _extract_meta_loosefolder(path: Path) -> dict:
    """Extract metadata for a loose song folder (raw XMLs + WEM audio)."""
    # Pass DLC_DIR so artist/album folder inference operates on the
    # dlc-relative path; otherwise absolute-path parts (e.g. the user's
    # home dir name) would leak into metadata for songs placed shallow
    # inside DLC_DIR.
    meta = loosefolder_mod.extract_meta(path, dlc_root=_get_dlc_dir())
    offsets = meta.pop("tuning_offsets", None) or [0] * 6
    name = tuning_name(offsets)
    meta["tuning"] = name
    meta["tuning_name"] = name
    meta["tuning_sort_key"] = sum(offsets)
    meta["format"] = "loose"
    meta.setdefault("stem_ids", [])
    # The library helper exposes absolute filesystem paths for audio/art
    # so callers inside the server can resolve them. Strip these before
    # the meta enters the API/DB cache — `/api/song/{filename}` returns
    # the dict directly on a cache miss, which would otherwise leak
    # `/home/<user>/...` paths to the frontend.
    meta.pop("audio_path", None)
    meta.pop("art_path", None)
    return meta


def _extract_meta_for_file(psarc_path: Path) -> dict:
    """Extract metadata — dispatches on extension; PSARC path tries fast then falls back."""
    # Sloppak is detected by `.sloppak` suffix only (cheap), so check it
    # first — that way a user's loose folder named `foo.sloppak` still wins
    # the sloppak branch instead of being misclassified.
    if sloppak_mod.is_sloppak(psarc_path):
        return _extract_meta_sloppak(psarc_path)
    if loosefolder_mod.is_loose_song(psarc_path):
        return _extract_meta_loosefolder(psarc_path)
    try:
        meta = _extract_meta_fast(psarc_path)
        if meta["title"]:
            return meta
    except Exception:
        pass
    # Fallback: full extraction (handles SNG-only official DLC etc.)
    tmp = tempfile.mkdtemp(prefix="rs_scan_")
    try:
        unpack_psarc(str(psarc_path), tmp)
        song = load_song(tmp)
        tuning = "E Standard"
        tuning_offsets: list[int] = [0] * 6
        if song.arrangements and song.arrangements[0].tuning:
            tuning_offsets = list(song.arrangements[0].tuning)
            tuning = tuning_name(tuning_offsets)
        arrangements = [
            {"index": i, "name": a.name,
             "notes": len(a.notes) + sum(len(c.notes) for c in a.chords)}
            for i, a in enumerate(song.arrangements)
        ]
        has_lyrics = False
        for xf in Path(tmp).rglob("*.xml"):
            try:
                if ET.parse(str(xf)).getroot().tag == "vocals":
                    has_lyrics = True
                    break
            except Exception:
                pass
        return {
            "title": song.title, "artist": song.artist,
            "album": song.album, "year": str(song.year) if song.year else "",
            "duration": song.song_length, "tuning": tuning,
            "arrangements": arrangements, "has_lyrics": has_lyrics,
            "stem_ids": [],
            "tuning_name": tuning,
            "tuning_sort_key": sum(tuning_offsets),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_SCAN_STATUS_INIT = {"running": False, "stage": "idle", "total": 0, "done": 0, "current": "", "error": None, "is_first_scan": False}
_scan_status = dict(_SCAN_STATUS_INIT)

_STARTUP_STATUS_INIT = {
    "running": True,
    "phase": "booting",
    "message": "Starting Slopsmith server...",
    "current_plugin": "",
    "loaded": 0,
    "total": 0,
    "error": None,
}
_startup_status = dict(_STARTUP_STATUS_INIT)
_startup_status_lock = threading.Lock()

_startup_sse_subscribers: set[asyncio.Queue] = set()
# threading.Lock (not asyncio.Lock) — also acquired from background threads
# in _notify_startup_sse; held only for set mutations (microseconds).
_startup_sse_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None

_SSE_POLL_INTERVAL = 2.0    # seconds: idle wait between disconnect checks
_SSE_KA_INTERVAL = 15.0     # seconds: interval between SSE keepalive data events


def _set_startup_status(**updates):
    global _startup_status
    with _startup_status_lock:
        next_status = dict(_startup_status)
        next_status.update(updates)
        _startup_status = next_status
        snapshot = dict(next_status)
    _notify_startup_sse(snapshot)


def _put_latest(q: asyncio.Queue, snapshot: dict) -> None:
    """Coalescing put: drain any stale snapshot then put the newest one.

    Because the queue is bounded to maxsize=1 and this function runs on the
    event loop, consecutive rapid updates replace the queued snapshot with
    the latest state rather than growing an unbounded backlog.
    """
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    try:
        q.put_nowait(snapshot)
    except asyncio.QueueFull:
        pass  # shouldn't happen after draining, but be defensive


def _notify_startup_sse(snapshot: dict) -> None:
    loop = _event_loop
    if loop is None or loop.is_closed():
        return
    with _startup_sse_lock:
        for q in _startup_sse_subscribers:
            try:
                loop.call_soon_threadsafe(_put_latest, q, snapshot)
            except RuntimeError:
                # Loop is closing (shutdown race); all remaining subscribers are
                # on the same loop and equally unreachable — break is correct.
                break


def _get_startup_status():
    with _startup_status_lock:
        return dict(_startup_status)


def _background_scan():
    """Scan all PSARCs and cache metadata on startup. Uses thread pool for parallelism.

    Never sets `_scan_status["running"] = False` — ownership of that flag
    lives in `_scan_runner` so a `_kick_scan()` racing this function's
    terminal write cannot observe a stale False and start a second runner.
    """
    global _scan_status
    _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "listing"}

    # Load config once so both the DLC-dir lookup and the platform filter
    # read from the same snapshot, avoiding a redundant parse of config.json.
    _cfg = _load_config(CONFIG_DIR / "config.json") or _default_settings()
    dlc = _get_dlc_dir(_cfg)
    if not dlc:
        _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "idle", "error": "DLC folder not configured"}
        log.warning("Scan: no DLC folder configured")
        return

    # Listing can fail on macOS without Full Disk Access, or on Docker if the
    # path isn't shared. Report the failure explicitly rather than silently
    # appearing to scan nothing.
    try:
        # Skip RS1 compatibility mega-PSARCs (multi-song, not individually playable)
        psarcs = [f for f in sorted(dlc.rglob("*.psarc"))
                  if f.is_file()
                  and "rs1compatibility" not in f.name.lower()]
        # Filter by platform suffix (_p.psarc = PC, _m.psarc = Mac) when the
        # user's DLC folder contains both variants of every song (e.g. a shared
        # Steam library between Windows and Mac).
        _platform = _cfg.get("psarc_platform", "both")
        if _platform == "pc":
            psarcs = [f for f in psarcs if not f.stem.endswith("_m")]
        elif _platform == "mac":
            psarcs = [f for f in psarcs if not f.stem.endswith("_p")]
        # Sloppaks: match both file (zip) and directory form by suffix.
        sloppaks = [f for f in sorted(dlc.rglob("*.sloppak"))
                    if sloppak_mod.is_sloppak(f)]

        # Loose song folders: any directory containing a non-preview *.wem + *.xml.
        # Skip directories that are actually sloppak bundles — those are
        # already in `sloppaks`; the dispatcher's sloppak-first precedence
        # would route them to the sloppak path anyway, but adding them
        # here would inflate the scan queue and over-count the total.
        loose_songs = []
        seen_loose = set()
        sloppak_dirs = {p for p in sloppaks if p.is_dir()}
        for wem in sorted(dlc.rglob("*.wem")):
            if "preview" in wem.stem.lower():
                continue
            d = wem.parent
            if d in sloppak_dirs or d.name.lower().endswith(".sloppak"):
                continue
            if d not in seen_loose and loosefolder_mod.is_loose_song(d):
                loose_songs.append(d)
                seen_loose.add(d)
    except PermissionError as e:
        msg = (f"Permission denied reading {dlc}. "
               "On macOS: grant Full Disk Access to the app in System Settings → Privacy & Security. "
               "With Docker: share this path in Docker Desktop → Settings → Resources → File Sharing.")
        log.error("Scan failed: %s (%s)", msg, e)
        _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "error", "error": msg}
        return
    except OSError as e:
        log.error("Scan failed listing %s: %s", dlc, e)
        _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "error", "error": f"Unable to list {dlc}: {e}"}
        return

    all_songs = psarcs + sloppaks + loose_songs
    log.info("Scan: listed %d PSARCs, %d sloppaks and %d loose folders in %s",
             len(psarcs), len(sloppaks), len(loose_songs), dlc)

    def _rel(f: Path) -> str:
        # Store the path relative to the DLC root so sub-folders (e.g.
        # dlc/sloppak/foo.sloppak produced by the converter) resolve back
        # correctly later. PSARCs always live directly in dlc/, so this
        # reduces to f.name for them.
        try:
            return f.relative_to(dlc).as_posix()
        except ValueError:
            return f.name

    current_files = {_rel(f) for f in all_songs}

    # Clean up stale DB entries
    stale = meta_db.delete_missing(current_files)
    if stale:
        log.info("Removed %d stale DB entries", stale)

    # Figure out which need scanning
    to_scan = []
    for f in all_songs:
        # Skip entries that vanish or become unreadable between listing
        # and stat. Without this, one concurrent move/delete in DLC_DIR
        # would crash the scan thread and leave `_scan_status["running"]`
        # stuck true with no path to recover.
        try:
            mtime, size = _stat_for_cache(f)
        except OSError as e:
            log.debug("scan: skipping %s (%s)", f, e)
            continue
        if not meta_db.get(_rel(f), mtime, size):
            to_scan.append((f, mtime, size))

    if not to_scan:
        _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "complete"}
        log.info("Scan: nothing new to scan (%d songs, all cached)", len(all_songs))
        return

    # Refine: all discovered songs need scanning → treat as first-time import
    # (covers moved DLC folder / fully-stale DB as well as a genuinely empty DB).
    is_first_scan = bool(all_songs) and len(to_scan) == len(all_songs)
    _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "scanning", "total": len(to_scan),
                    "is_first_scan": is_first_scan}
    log.info("Library: %d PSARCs + %d sloppaks + %d loose folders, %d cached, %d to scan",
             len(psarcs), len(sloppaks), len(loose_songs), len(all_songs) - len(to_scan), len(to_scan))

    def _scan_one(item):
        f, mtime, size = item
        # Per-file log so users running the server / desktop can see live
        # activity and distinguish a stuck scan from a slow one.
        log.debug("scanning %s", f.name)
        meta = _extract_meta_for_file(f)
        return _rel(f), mtime, size, meta

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_scan_one, item): item[0].name for item in to_scan}
        for future in concurrent.futures.as_completed(futures):
            fname = futures[future]
            try:
                name, mtime, size, meta = future.result()
                meta_db.put(name, mtime, size, meta)
            except Exception as e:
                log.warning("scan failed for %s: %s", fname, e)
            _scan_status["done"] += 1
            _scan_status["current"] = fname

    log.info("Scan complete: %d songs cached", len(to_scan))
    _scan_status = {**_SCAN_STATUS_INIT, "running": True, "stage": "complete"}


_scan_kick_lock = threading.Lock()
_scan_rescan_pending = False


def _kick_scan() -> bool:
    """Request a library rescan, single-flight + coalescing.

    Returns True if a new scan thread was started, False if one was already
    running. In the latter case a follow-up pass is queued and runs as soon
    as the current scan finishes so files landing mid-scan (e.g. an upload
    that finalizes after the scan has already listed DLC_DIR) are not lost
    until the next periodic pass. Multiple late-arriving requests coalesce
    into a single follow-up.
    """
    global _scan_rescan_pending
    with _scan_kick_lock:
        if _scan_status["running"]:
            _scan_rescan_pending = True
            return False
        # Mark running synchronously so a parallel _kick_scan() observes it
        # before the worker thread has a chance to reassign _scan_status.
        _scan_status["running"] = True
    threading.Thread(target=_scan_runner, daemon=True).start()
    return True


def _scan_runner():
    """Run _background_scan, then re-run if requests arrived mid-scan."""
    global _scan_rescan_pending
    while True:
        try:
            _background_scan()
        except Exception:
            log.exception("background scan failed unexpectedly")

        with _scan_kick_lock:
            if not _scan_rescan_pending:
                _scan_status["running"] = False
                return
            _scan_rescan_pending = False
            _scan_status["running"] = True


# ── Register plugin API endpoints (lightweight, before app starts) ───────────
from plugins import load_plugins, register_plugin_api
register_plugin_api(app)

# Plugin loading deferred to startup event (see below) to avoid blocking
# server startup when many plugins are installed.


@app.on_event("startup")
async def startup_events():
    # Safety net: re-apply the structlog pipeline in case the server was
    # started directly via `uvicorn server:app` (without main.py).  When
    # running via `python main.py`, configure_logging() was already called
    # before uvicorn.run(..., log_config=None), so uvicorn never calls its
    # own dictConfig() and this call is effectively a no-op.  When running
    # the uvicorn CLI directly, uvicorn applies LOGGING_CONFIG before the
    # ASGI startup hook fires, overwriting the uvicorn* handlers; this call
    # restores them for all messages after "Waiting for application startup".
    configure_logging()

    loop = asyncio.get_running_loop()
    global _event_loop
    _event_loop = loop

    # Test/CI escape hatch: tests that import the FastAPI app via TestClient
    # don't need plugin loading or the background library scan, and those
    # paths touch the user filesystem in ways that aren't safe under
    # parallel test runs. Drive startup straight to a terminal "complete"
    # phase so any frontend startup waiter that observes the lifespan also
    # unblocks cleanly (the SSE/poll client treats only `complete` and
    # `error` as terminal when `running` becomes false).
    if _env_flag("SLOPSMITH_SKIP_STARTUP_TASKS"):
        log.info("[startup] Skipping plugin load and background scan")
        # Tests pop `server` from sys.modules across runs, but the `plugins`
        # module is not reloaded — so LOADED_PLUGINS can carry stale entries
        # from a previous test's startup, which `/api/plugins` would then
        # expose despite this branch reporting zero loaded plugins. Normal
        # startup clears it inside load_plugins; do the same here under the
        # same lock so this skip path matches that invariant.
        from plugins import LOADED_PLUGINS, PLUGINS_LOCK
        with PLUGINS_LOCK:
            LOADED_PLUGINS.clear()
        _set_startup_status(
            running=False,
            phase="complete",
            message="Startup tasks skipped (SLOPSMITH_SKIP_STARTUP_TASKS).",
            error=None,
            current_plugin="",
            loaded=0,
            total=0,
        )
        return

    # Sweep stranded PSARC/Demucs staging dirs from any previous run
    # that was SIGKILL'd mid-conversion. lib/sloppak_convert.py wraps
    # each conversion in `tempfile.mkdtemp(prefix="s2p_extract_")` /
    # `tempfile.TemporaryDirectory(prefix="s2p_split_")` (and other
    # `s2p_*` variants); cleanup runs only in the normal `finally` /
    # `__exit__` path —
    # kills (Docker shutdown timeout, OOM, `docker compose restart`
    # mid-job) leak the staging dir, and bulk-converts can leave many
    # GB across restarts. Run before plugin load so the sloppak-
    # converter plugin's worker starts on a clean `/tmp` even if the
    # previous server died holding extractions. Sits AFTER the
    # SLOPSMITH_SKIP_STARTUP_TASKS escape hatch so test runs that
    # need a true filesystem no-op still get one.
    try:
        from sloppak_convert import cleanup_stale_temp_dirs
        # 15 minutes — safety margin chosen to cover the worst-case
        # file-write gap of any routine in `lib/sloppak_convert.py`.
        # The remote Demucs path (`_run_demucs_remote`) uploads the
        # audio and then polls the server for up to 10 minutes
        # (`for _ in range(120): time.sleep(5)`) before downloading
        # the stems; during that poll no files are written under the
        # staging dir. A concurrent server restart at the 5-minute
        # mark would see an `s2p_split_*` dir with no descendants
        # touched in >300s and delete it — breaking the live job when
        # downloads start. 900s (15 min) clears the full 10-min poll
        # plus a generous margin for upload and download time.
        # For local Demucs / PSARC / WEM routines, which write
        # continuously, the recursive mtime check correctly keeps
        # active dirs alive; the 15-min threshold is still far shorter
        # than the weeks a truly stranded dir would accumulate.
        # Cost of being conservative: a kill less than 15 minutes
        # before *this* restart leaves its staging dir for the next
        # startup pass — fine; the next sweep catches it.
        cleanup_stale_temp_dirs(min_age_seconds=900.0)
    except Exception:
        # `log.exception` (vs `log.warning(... %s, e)`) preserves the
        # traceback — useful for distinguishing import errors,
        # permission denials, and runtime failures inside the helper.
        log.exception("startup temp-dir cleanup failed")

    _set_startup_status(
        running=True,
        phase="starting",
        message="Core server ready. Starting plugin loader...",
        error=None,
    )

    plugin_context = {
        "config_dir": CONFIG_DIR,
        "get_dlc_dir": _get_dlc_dir,
        "extract_meta": _extract_meta_for_file,
        "meta_db": meta_db,
        "get_sloppak_cache_dir": lambda: SLOPPAK_CACHE_DIR,
        "register_demo_janitor_hook": register_demo_janitor_hook,
    }

    # Load plugins asynchronously so HTTP routes and the desktop window can
    # come up immediately while heavy plugin imports/install steps continue.
    _sync_mode = os.environ.get("SLOPSMITH_SYNC_STARTUP", "").lower() in {"1", "true", "yes", "on"}

    def _load_plugins_background():
        try:
            # Track all active plugin errors so that a `clear_error=True`
            # event from a fallback recovery correctly restores any *other*
            # plugin's still-unresolved failure rather than wiping the error
            # field entirely.
            #
            # Using a single "last error" pointer was insufficient: if plugin A
            # fails, then plugin B fails and later recovers, the recovery would
            # overwrite the pointer with B's id — and then B's `error=None`
            # clears the status to null even though A is still broken.
            #
            # With a dict (keyed by plugin_id, insertion-ordered) we can
            # remove B's entry on recovery and restore the most recent remaining
            # failure from A, giving an accurate picture of startup health.
            _active_errors: dict[str, str] = {}  # plugin_id -> error text

            def _on_progress(event: dict):
                total = int(event.get("total") or 0)
                loaded = int(event.get("loaded") or 0)
                plugin_id = event.get("plugin_id") or ""
                message = event.get("message") or "Loading plugins..."
                phase = event.get("phase") or "plugins-loading"
                update: dict = dict(
                    running=True,
                    phase=phase,
                    message=message,
                    current_plugin=plugin_id,
                    loaded=loaded,
                    total=total,
                )
                # Forward the error field only when the event explicitly
                # carries it.  Two cases:
                # - Non-null string: record this plugin's failure and display it.
                # - Explicit null (clear_error=True in _emit_progress):
                #   remove this plugin's failure entry, then restore the most
                #   recently recorded still-active failure (if any) so
                #   unresolved failures from other plugins remain visible.
                #   An unscoped clear (no plugin_id) removes the unscoped
                #   sentinel and applies the same restore logic.
                # Events that omit the key entirely leave the status unchanged,
                # preserving any earlier plugin error across the many
                # non-error progress events that follow normal setup steps.
                if "error" in event:
                    err_val = event["error"]
                    if err_val is not None:
                        # Pop then re-insert so the key moves to the end of
                        # insertion order even when this plugin already has an
                        # entry.  A plugin can emit more than one error during a
                        # single load (requirements + routes), and dict.update()
                        # on an existing key does NOT move it to the end, so
                        # remaining[-1] could return a stale earlier message
                        # after another plugin clears its own error.
                        _active_errors.pop(plugin_id, None)
                        _active_errors[plugin_id] = err_val
                        update["error"] = err_val
                    else:
                        # Clear this plugin's error entry (fallback recovery or
                        # unscoped clear), then surface the most recently added
                        # remaining failure, or None if all have been resolved.
                        _active_errors.pop(plugin_id, None)
                        remaining = list(_active_errors.values())
                        update["error"] = remaining[-1] if remaining else None
                _set_startup_status(**update)

            def _route_setup_on_main(fn):
                """Schedule plugin route registration on the event-loop thread.

                FastAPI/Starlette router mutation is not thread-safe, so the
                actual setup() call is normally marshalled back onto the event
                loop via call_soon_threadsafe.  The background thread blocks
                until the registration completes, raises, or a 60 s timeout
                elapses.

                In synchronous startup mode (_sync_mode=True) this function is
                called directly from the event-loop thread, so marshalling via
                call_soon_threadsafe + fut.result() would deadlock (the loop
                cannot drain the queued callback while it is blocked here).
                In that case fn() is invoked inline instead.

                On timeout (async mode only), startup continues normally.  Any
                exception that eventually arrives is logged via a done-callback
                so it is never silently dropped.
                """
                if _sync_mode:
                    # Already on the event-loop thread — call directly.
                    fn()
                    return

                fut: concurrent.futures.Future = concurrent.futures.Future()
                # _state_lock makes the "check _cancelled + set _started"
                # transition in _do() atomic with the "read _started + set
                # _cancelled" transition in the timeout handler.  Without this
                # lock the two threads can interleave:
                #
                #   Thread A (_do):   passes check-1, yields to event loop
                #   Thread B (timeout): reads _started=False → _mid_flight=False
                #   Thread A (_do):   sets _started, passes check-2 → calls fn()
                #   Thread B (timeout): sets _cancelled (too late)
                #   Result: fn() runs AND fallback loads — concurrent mutation.
                #
                # With the lock, either _do() commits to running fn() before
                # the timeout can set _cancelled (in which case _mid_flight=True
                # and the fallback is skipped), or the timeout wins (sets
                # _cancelled=True and reads _started=False → _mid_flight=False,
                # then _do() sees _cancelled inside the lock and bails out).
                _state_lock = threading.Lock()
                _cancelled = threading.Event()
                _started = threading.Event()

                def _do():
                    with _state_lock:
                        if _cancelled.is_set():
                            # Timeout already fired before we started; bail
                            # to prevent a race with any fallback that may
                            # have been activated by load_plugins().
                            if not fut.done():
                                fut.set_result(None)
                            return
                        _started.set()
                    # Past the lock — committed to running fn().
                    try:
                        fn()
                        fut.set_result(None)
                    except Exception as exc:
                        fut.set_exception(exc)

                loop.call_soon_threadsafe(_do)
                try:
                    fut.result(timeout=60)
                except concurrent.futures.TimeoutError as _te:
                    _pid = getattr(fn, "_plugin_id", "unknown")
                    # Read _started and set _cancelled atomically so _do()
                    # can't slip through the lock and start fn() between the
                    # two operations.
                    with _state_lock:
                        _mid_flight = _started.is_set()
                        _cancelled.set()
                    if _mid_flight:
                        log.warning(
                            "route registration for %r timed out after 60 s and "
                            "setup() was already mid-flight; any routes registered "
                            "before the timeout cannot be removed. The user-copy "
                            "fallback will NOT be activated to prevent concurrent "
                            "router mutation (Python threads cannot be interrupted "
                            "mid-execution). Restart the server to recover.",
                            _pid,
                        )
                        # Signal to load_plugins() that fallback is unsafe
                        # for this plugin — the original setup() is still
                        # running and may add more routes concurrently.
                        _te.setup_mid_flight = True
                    else:
                        log.warning(
                            "route registration for %r timed out after 60 s; "
                            "setup() had not started yet, so it has been cancelled "
                            "and the user-copy fallback (if any) can proceed safely.",
                            _pid,
                        )
                    # Prevent the still-queued _do() from executing if it
                    # hasn't started yet — avoids races with any fallback.
                    # Note: _cancelled was already set inside _state_lock above.

                    def _log_deferred(f: concurrent.futures.Future):
                        try:
                            exc = f.exception()
                        except concurrent.futures.CancelledError:
                            return
                        if exc is not None:
                            log.error("deferred route registration for %r raised: %s", _pid, exc)

                    fut.add_done_callback(_log_deferred)
                    raise  # propagate to load_plugins() so it emits plugin-error and skips "Loaded routes"

            _set_startup_status(
                running=True,
                phase="plugins-loading",
                message="Loading plugins...",
                current_plugin="",
                loaded=0,
                total=0,
                error=None,
            )
            load_plugins(app, plugin_context, progress_cb=_on_progress,
                         route_setup_fn=_route_setup_on_main)
            status = _get_startup_status()
            _set_startup_status(
                running=False,
                phase="complete",
                message="Startup complete",
                current_plugin="",
                loaded=status.get("loaded", 0),
                total=max(status.get("total", 0), status.get("loaded", 0)),
                error=status.get("error"),
            )
        except Exception as e:
            _set_startup_status(
                running=False,
                phase="error",
                message="Plugin startup failed",
                error=str(e),
            )
            log.exception("plugin startup failed")

    if _sync_mode:
        # Caller requested synchronous startup (e.g. test environment).
        # Run the loader inline so startup is complete before the server's
        # startup handler returns — no polling or timing workarounds needed.
        _load_plugins_background()
    else:
        threading.Thread(target=_load_plugins_background, daemon=True).start()

    global _DEMO_JANITOR_STARTED, _DEMO_JANITOR_THREAD
    if os.environ.get("SLOPSMITH_DEMO_MODE") == "1" and not _DEMO_JANITOR_STARTED:
        _DEMO_JANITOR_STARTED = True
        _DEMO_JANITOR_STOP.clear()
        def _janitor():
            while not _DEMO_JANITOR_STOP.wait(timeout=3600):
                with _DEMO_JANITOR_HOOKS_LOCK:
                    hooks = list(_DEMO_JANITOR_HOOKS)
                for hook in hooks:
                    _run_janitor_hook(hook)
        _DEMO_JANITOR_THREAD = threading.Thread(target=_janitor, daemon=True, name="demo-janitor")
        _DEMO_JANITOR_THREAD.start()

    # Start background metadata scan
    startup_scan()


@app.on_event("shutdown")
def shutdown_events():
    """Stop the demo-mode janitor thread (if running) on server shutdown."""
    global _DEMO_JANITOR_STARTED, _DEMO_JANITOR_THREAD, _event_loop
    _event_loop = None  # prevent stale loop reference after shutdown
    if _DEMO_JANITOR_STARTED:
        _DEMO_JANITOR_STOP.set()
        thread = _DEMO_JANITOR_THREAD
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                import warnings
                warnings.warn(
                    "demo-janitor thread did not stop within 5 s; "
                    "a registered hook may be blocking",
                    RuntimeWarning,
                    stacklevel=1,
                )
                # Leave _DEMO_JANITOR_STARTED True so a new janitor is not
                # spawned by a subsequent startup while the old one is alive.
                return
            _DEMO_JANITOR_THREAD = None
        _DEMO_JANITOR_STARTED = False
        with _DEMO_JANITOR_HOOKS_LOCK:
            _DEMO_JANITOR_HOOKS.clear()


def startup_scan():
    """Start background metadata scan and periodic rescan on server start."""
    _kick_scan()
    # Periodic rescan every 5 minutes
    rescan_thread = threading.Thread(target=_periodic_rescan, daemon=True)
    rescan_thread.start()


def _periodic_rescan():
    """Check for new files every 5 minutes."""
    time.sleep(300)  # Wait 5 minutes after startup
    while True:
        # _kick_scan() is a no-op (returns False, queues a pending pass) when
        # a scan is already running, so racing against the active scan is
        # safe — no second runner is spawned.
        _kick_scan()
        time.sleep(300)


def _safe_http_url(raw):
    """Return `raw` stripped + trailing-slash-stripped if it parses as an
    http(s) URL with a non-empty host; else None.

    Used to validate operator-supplied `APP_SOURCE_URL` / `APP_LICENSE_URL`
    env vars before they reach `<a href>` in the UI. A bare prefix check
    like `startswith(("http://","https://"))` accepts malformed inputs
    such as `"https://"` (no host) or `"https:///foo"` (empty host) that
    still produce broken hrefs — and, when used as a base for the default
    `license_url`, garbage like `"https:///blob/main/LICENSE"`.
    """
    from urllib.parse import urlsplit
    if not raw:
        return None
    s = raw.strip().rstrip("/")
    if not s:
        return None
    try:
        parsed = urlsplit(s)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    # `netloc` includes any `user:pass@` and `:port` — strings like
    # "http://:80/path" have non-empty netloc (":80") but no real
    # hostname. Validate `hostname` so only URLs with an actual host
    # are accepted.
    if not parsed.hostname:
        return None
    return s


@app.get("/api/version")
def get_version():
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        version = env_version
    else:
        version_file = Path(__file__).parent / "VERSION"
        version = "unknown"
        if version_file.exists():
            try:
                version = version_file.read_text().strip()
            except (OSError, UnicodeDecodeError):
                pass
    default_source_url = "https://github.com/byrongamatos/slopsmith"
    # APP_SOURCE_URL / APP_LICENSE_URL flow straight into <a href> in the UI,
    # so validate with urllib.parse rather than a bare prefix check — a prefix
    # check accepts malformed values like "https://" (no host) which produce
    # broken hrefs (and a constructed license_url like "https:///blob/main/LICENSE").
    # _safe_http_url requires scheme in {http,https} AND a non-empty hostname
    # (not just netloc — that would still accept port-only authorities like
    # "http://:80/path"); fall back to the safe default otherwise.
    source_url = _safe_http_url(os.environ.get("APP_SOURCE_URL")) or default_source_url
    # APP_LICENSE_URL: explicit override for the LICENSE link. The default
    # constructed value (source_url + "/blob/main/LICENSE") is GitHub-
    # specific and assumes the repo's default branch is `main`; non-GitHub
    # hosts (GitLab, Gitea, self-hosted) need an explicit value.
    license_url = _safe_http_url(os.environ.get("APP_LICENSE_URL")) or (source_url + "/blob/main/LICENSE")
    return {
        "version": version,
        "source_url": source_url,
        "license_url": license_url,
    }


@app.get("/api/scan-status")
def scan_status():
    return _scan_status


@app.get("/api/startup-status")
def startup_status():
    return _get_startup_status()


@app.get("/api/startup-status/stream")
async def startup_status_stream(request: Request):
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    # Register before putting the initial snapshot.  asyncio cooperative
    # scheduling guarantees _put_latest cannot run between add() and the
    # put() below: put() on an empty maxsize-1 queue never yields (CPython
    # fast path), so no event-loop iteration fires in between.  Registering
    # first ensures a terminal status fired just after connect is never missed.
    with _startup_sse_lock:
        _startup_sse_subscribers.add(queue)
    await queue.put(_get_startup_status())

    async def _gen():
        since_ka = 0.0
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=_SSE_POLL_INTERVAL)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    since_ka += _SSE_POLL_INTERVAL
                    if since_ka >= _SSE_KA_INTERVAL:
                        yield 'data: {"type":"keepalive"}\n\n'
                        since_ka = 0.0
                    continue
                yield f"data: {json.dumps(data)}\n\n"
                if not data.get("running", True):
                    break
                since_ka = 0.0  # reset keepalive timer — a real event just went out
                # Check after each delivered message so that rapid-fire updates
                # don't prevent disconnect detection (the timeout path above only
                # fires when the queue is idle for the full _SSE_POLL_INTERVAL).
                if await request.is_disconnected():
                    break
        finally:
            with _startup_sse_lock:
                _startup_sse_subscribers.discard(queue)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/rescan")
def trigger_rescan():
    """Manually trigger a library rescan."""
    if not _kick_scan():
        return {"message": "Scan already in progress"}
    return {"message": "Rescan started"}


@app.post("/api/rescan/full")
def trigger_full_rescan():
    """Clear cache and rescan everything."""
    if _scan_status["running"]:
        return {"message": "Scan already in progress"}
    with meta_db._lock:
        meta_db.conn.execute("DELETE FROM songs")
        meta_db.conn.commit()
    if not _kick_scan():
        return {"message": "Scan already in progress"}
    return {"message": "Full rescan started"}


# ── Song upload ───────────────────────────────────────────────────────────────

_ALLOWED_SONG_EXTS = {".psarc", ".sloppak"}
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB — covers sloppaks bundled with stems
# Per-request batch cap. Lets a user drop a whole album of sloppaks at once
# without giving a hostile client a 1000-file DoS surface via Starlette's
# default max_files=1000. The pre-parse Content-Length guard is sized as
# _MAX_UPLOAD_FILES * _MAX_UPLOAD_BYTES + slack.
_MAX_UPLOAD_FILES = 50
# Multipart Content-Length includes boundary markers + per-part headers, so a
# file sitting right at _MAX_UPLOAD_BYTES would be rejected by an equality cap
# on Content-Length. Add a generous slack for the multipart envelope; the real
# file-size cap is enforced by the streaming check in _save_uploaded_song().
_MULTIPART_OVERHEAD_SLACK = 1024 * 1024  # 1 MiB
# Serializes the mutating step of upload (os.replace into DLC_DIR) with
# delete_song so the two endpoints can't interleave on the same path —
# e.g. an upload finishing right after a concurrent delete shouldn't
# resurrect a song the user just removed, and a delete arriving mid-
# overwrite shouldn't strand a half-written file. threading.Lock (not
# asyncio.Lock) because delete_song is sync (runs in the threadpool);
# upload acquires it inside ``run_in_threadpool`` for the same reason.
_song_io_lock = threading.Lock()


def _commit_uploaded_song(tmp_path: Path, dest: Path, overwrite: bool, base: str):
    """Atomically move a validated temp upload into ``dest`` under ``_song_io_lock``.

    Returns ``None`` on success or an error result dict matching the upload
    endpoint's contract. Holds the lock across the directory re-check and
    the final ``os.replace`` so a concurrent delete or upload can't slip
    between them. Always cleans up the temp file on the error paths.
    """
    with _song_io_lock:
        if dest.exists():
            if not overwrite:
                # Lost the race against a concurrent upload of the same name.
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                return {"status": "exists", "filename": base,
                        "error": "A file with this name already exists"}
            # Re-check directory state under the lock — the pre-check
            # may have raced an unrelated mkdir, and a sloppak directory
            # has to be removed before os.replace() can write over it.
            if dest.is_dir():
                if not sloppak_mod.is_sloppak(dest):
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                    return {"status": "exists", "filename": base,
                            "error": "A directory with this name exists and is not "
                                     "a sloppak — refusing to overwrite"}
                shutil.rmtree(str(dest))
        os.replace(str(tmp_path), str(dest))
    return None


def _invalidate_song_caches(cache_key: str) -> None:
    """Drop filename-keyed derived caches when a song at ``cache_key`` is
    replaced or removed. Sloppak's ``_source_cache`` and loose-folder audio
    IDs self-invalidate via stat checks; the caches purged here do not."""
    # In-memory PSARC extraction cache (filename → tmp dir + Song).
    with _extract_cache_lock:
        stale = _extract_cache.pop(cache_key, None)
    if stale:
        shutil.rmtree(stale[0], ignore_errors=True)

    # Art cache — match the safe_name mapping used by get_song_art /
    # upload_song_art_b64 exactly so we hit the same on-disk file.
    safe_name = cache_key.replace("/", "_").replace(" ", "_")
    art_file = ART_CACHE_DIR / f"{safe_name}.png"
    try:
        art_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to evict art cache for %s", cache_key, exc_info=True)

    # PSARC audio cache — audio_id is `Path(filename).stem.replace(" ", "_")`
    # without any stat digest, so a same-named replacement would serve the
    # previous file's converted audio. Loose-folder ids include a wem stat
    # digest and self-heal; sloppak streams stems directly and uses no
    # audio_id at all — both safely no-op here.
    audio_id = Path(cache_key).stem.replace(" ", "_")
    for d in (AUDIO_CACHE_DIR, STATIC_DIR):
        for ext in (".mp3", ".ogg", ".wav"):
            f = d / f"audio_{audio_id}{ext}"
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("failed to evict audio cache file %s", f, exc_info=True)


@app.post("/api/songs/upload")
async def upload_song(request: Request):
    """Upload one or more .psarc / .sloppak files into the configured DLC folder.

    Multipart body with one or more ``file`` fields (up to ``_MAX_UPLOAD_FILES``
    per request). Query string:
      ``overwrite=1`` — replace existing files with the same name.

    Response shape (always HTTP 200 once we've gotten past request-level guards
    like DLC-not-configured / payload-too-large):
      ``{"results": [{"filename": "...", "status": "ok" | "exists" | "error",
                       "error"?: "...", "size"?: N, "format"?: "psarc"}, ...]}``
    Per-file conflicts surface as ``status: "exists"`` so a batch upload can
    surface ALL conflicts at once instead of bailing on the first one. The
    client re-POSTs just the conflicting files with ``overwrite=1`` if the
    user opts in.

    The DLC directory is resolved via ``_get_dlc_dir()`` which honours the
    ``DLC_DIR`` env var first and falls back to ``dlc_dir`` in
    ``config.json`` — so uploads land in whichever folder the rest of the
    app already considers the library root, regardless of which mechanism
    configured it.
    """
    dlc = _get_dlc_dir()
    if dlc is None:
        return JSONResponse(
            {"error": "DLC folder is not configured. Set DLC_DIR or configure it in Settings."},
            status_code=503,
        )
    if not os.access(str(dlc), os.W_OK):
        return JSONResponse(
            {"error": f"DLC folder {dlc} is not writable by the server process."},
            status_code=500,
        )

    # Pre-parse Content-Length guard — fail fast before reading any body.
    # Multipart Content-Length is file bytes + boundary + per-part headers, so
    # we can't use _MAX_UPLOAD_BYTES as an exact cap here (a file right at the
    # advertised max would be rejected before _save_uploaded_song() can apply
    # the real per-file byte cap). For batch uploads we allow up to
    # _MAX_UPLOAD_FILES files at _MAX_UPLOAD_BYTES each; the parser still
    # enforces per-part size via max_part_size and per-batch count via
    # max_files. The streaming check inside _save_uploaded_song() is the
    # authoritative per-file size cap.
    max_total = _MAX_UPLOAD_FILES * _MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_SLACK
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            cl_int = int(cl)
        except ValueError:
            return JSONResponse({"error": "Invalid Content-Length header"}, status_code=400)
        if cl_int < 0:
            return JSONResponse({"error": "Invalid Content-Length header"}, status_code=400)
        if cl_int > max_total:
            return JSONResponse(
                {"error": f"Batch upload exceeds {_MAX_UPLOAD_FILES} files × "
                          f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"},
                status_code=413,
            )

    overwrite = request.query_params.get("overwrite") == "1"
    # Tighten the parser to the handler's contract: up to _MAX_UPLOAD_FILES
    # file parts, no text parts (overwrite comes from query params).
    # Starlette's defaults of max_files=1000 / max_fields=1000 would
    # otherwise let a client force the parser to spool far more parts than
    # the endpoint is willing to process.
    form = await request.form(
        max_files=_MAX_UPLOAD_FILES,
        max_fields=0,
        max_part_size=_MAX_UPLOAD_BYTES,
    )
    try:
        from starlette.datastructures import UploadFile as _StarletteUploadFile
        # form.getlist("file") returns all parts named "file" in submission
        # order. Filter to file parts only — Starlette would yield strings
        # for text parts, but we've capped max_fields=0 so any non-file part
        # is already a parser error before reaching here.
        uploads = [u for u in form.getlist("file") if isinstance(u, _StarletteUploadFile)]
        if not uploads:
            return JSONResponse(
                {"error": "Expected one or more files in multipart field 'file'"},
                status_code=400,
            )

        results = []
        any_saved = False
        for upload in uploads:
            try:
                result = await _save_uploaded_song(upload, dlc, overwrite)
                results.append(result)
                if result.get("status") == "ok":
                    any_saved = True
            except Exception as e:
                # Per-file failure must not abort the batch — record and
                # continue so the client gets a complete report.
                log.exception("upload failed for %r", getattr(upload, "filename", "?"))
                results.append({
                    "filename": Path(getattr(upload, "filename", "") or "").name or "?",
                    "status": "error",
                    "error": f"Upload failed: {e}",
                })
            finally:
                try:
                    await upload.close()
                except Exception:
                    log.debug("failed to close upload file handle", exc_info=True)

        if any_saved:
            _kick_scan()
        return {"results": results}
    finally:
        try:
            await form.close()
        except Exception:
            log.debug("failed to close form", exc_info=True)


async def _save_uploaded_song(upload: UploadFile, dlc: Path, overwrite: bool) -> dict:
    """Save one upload into ``dlc``. Returns a per-file result dict (never
    a JSONResponse) so batch uploads can aggregate.

    Shape:
      ok:     ``{"status": "ok", "filename": base, "size": N, "format": "psarc"}``
      exists: ``{"status": "exists", "filename": base, "error": "..."}``
      error:  ``{"status": "error", "filename": base, "error": "..."}``
    """
    # Strip any path components a client may have included in the filename —
    # only the basename lands in the DLC root. Path traversal would otherwise
    # let a crafted upload escape the library directory.
    raw_name = upload.filename or ""
    base = Path(raw_name).name
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        return {"status": "error", "filename": raw_name or "?", "error": "Invalid filename"}
    suffix = Path(base).suffix.lower()
    if suffix not in _ALLOWED_SONG_EXTS:
        return {"status": "error", "filename": base,
                "error": "Only .psarc and .sloppak files are accepted"}

    dest = dlc / base
    if dest.exists():
        if not overwrite:
            return {"status": "exists", "filename": base,
                    "error": "A file with this name already exists"}
        # overwrite=1 must handle directory-form sloppaks (the scanner and
        # delete path both treat them as song entries). os.replace() can't
        # clobber a non-empty directory, so without the rmtree below the
        # whole upload would write to a temp file and then surface a late
        # 500 at the os.replace() call. Refuse other directories so an
        # unrelated folder isn't blown away by a same-named upload.
        if dest.is_dir() and not sloppak_mod.is_sloppak(dest):
            return {"status": "exists", "filename": base,
                    "error": "A directory with this name exists and is not a sloppak — "
                             "refusing to overwrite"}

    # Temp file in the DLC dir itself so os.replace is atomic (same filesystem).
    # Dot-prefix keeps it out of the rglob("*.psarc")/"*.sloppak") scan globs.
    fd, tmp_name = await run_in_threadpool(
        tempfile.mkstemp, dir=str(dlc), prefix=".upload-", suffix=".part"
    )
    tmp_path = Path(tmp_name)
    bytes_read = 0
    head = b""
    error_result: dict | None = None
    try:
        try:
            tmpf = await run_in_threadpool(os.fdopen, fd, "wb")
        except BaseException:
            try:
                await run_in_threadpool(os.close, fd)
            except OSError:
                pass
            raise
        try:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > _MAX_UPLOAD_BYTES:
                    error_result = {
                        "status": "error", "filename": base,
                        "error": f"Upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap",
                    }
                    break
                if len(head) < 4:
                    head += chunk[: 4 - len(head)]
                await run_in_threadpool(tmpf.write, chunk)
        finally:
            await run_in_threadpool(tmpf.close)

        if error_result is None:
            if bytes_read == 0:
                error_result = {"status": "error", "filename": base,
                                "error": "Empty upload — file is 0 bytes"}
            elif suffix == ".psarc" and head[:4] != b"PSAR":
                error_result = {"status": "error", "filename": base,
                                "error": "Not a valid PSARC file (wrong magic bytes)"}
            elif suffix == ".sloppak":
                if head[:2] != b"PK":
                    error_result = {"status": "error", "filename": base,
                                    "error": "Not a valid sloppak file (expected zip archive)"}
                else:
                    # ZIP magic alone admits any renamed zip — verify the sloppak
                    # loader can actually parse a manifest.yaml inside. Without
                    # this, /api/songs/upload returns "ok" for files the rest of
                    # the backend would refuse to scan or load.
                    try:
                        await run_in_threadpool(sloppak_mod.load_manifest, tmp_path)
                    except Exception as e:
                        error_result = {"status": "error", "filename": base,
                                        "error": f"Not a valid sloppak file: {e}"}

        if error_result is not None:
            try:
                await run_in_threadpool(tmp_path.unlink)
            except OSError:
                pass
            return error_result

        # Single sync helper so the lock is held for the whole commit —
        # ``async with _upload_lock`` would have released between every
        # ``run_in_threadpool`` and let a concurrent delete or upload slip
        # in between the dir check and the final ``os.replace``.
        commit_result = await run_in_threadpool(
            _commit_uploaded_song, tmp_path, dest, overwrite, base
        )
        if commit_result is not None:
            return commit_result
    except BaseException:
        try:
            await run_in_threadpool(tmp_path.unlink)
        except OSError:
            pass
        raise

    # Even on a fresh (non-overwrite) upload, evict any stale entries left
    # over from a previous delete+re-upload of the same name.
    await run_in_threadpool(_invalidate_song_caches, base)

    log.info("Uploaded %s (%d bytes) to %s", base, bytes_read, dlc)
    return {"status": "ok", "filename": base, "size": bytes_read,
            "format": suffix.lstrip(".")}


@app.delete("/api/song/{filename:path}")
def delete_song(filename: str):
    """Remove a song from the DLC folder and clear its cache entries.

    Works for all three formats: ``.psarc`` files, ``.sloppak`` files
    OR directories, and loose-folder songs (the directory containing the
    chart). The path is resolved through ``_resolve_dlc_path`` so URL-encoded
    ``..`` segments cannot escape the library root.
    """
    dlc = _get_dlc_dir()
    if dlc is None:
        return JSONResponse({"error": "DLC folder not configured"}, status_code=503)
    resolved = _resolve_dlc_path(dlc, filename)
    if resolved is None:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not resolved.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    if resolved == dlc.resolve():
        return JSONResponse({"error": "Refusing to delete the DLC root"}, status_code=400)

    # Only delete actual song entries. Without this, DELETE /api/song/ArtistName
    # would recursively wipe a whole artist subfolder — far broader than the
    # UI's per-song contract. Sloppak detection wins over loose because a
    # sloppak dir can also contain WEM/XML (matches the scanner's precedence).
    is_psarc = resolved.is_file() and resolved.suffix.lower() == ".psarc"
    is_sloppak = sloppak_mod.is_sloppak(resolved)
    is_loose = (
        resolved.is_dir()
        and not is_sloppak
        and loosefolder_mod.is_loose_song(resolved)
    )
    if not (is_psarc or is_sloppak or is_loose):
        return JSONResponse(
            {"error": "Not a song entry — only PSARC files, sloppaks, "
                      "or loose-folder songs can be deleted"},
            status_code=400,
        )

    # Hold ``_song_io_lock`` across the filesystem removal AND the DB/cache
    # eviction. Without it, an upload of the same filename could ``os.replace``
    # a new file into place between our removal and DB delete, leaving the
    # new generation stranded with no library row; or the reverse, where
    # delete runs between an upload's directory check and its replace and
    # the upload then resurrects the song we just removed.
    with _song_io_lock:
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
        except OSError as e:
            log.error("Failed to delete %s: %s", resolved, e)
            return JSONResponse({"error": f"Delete failed: {e}"}, status_code=500)

        # Canonicalise the cache key the same way update_song_meta does so we
        # hit the row the scanner indexed under.
        try:
            cache_key = resolved.relative_to(dlc.resolve()).as_posix()
        except ValueError:
            cache_key = filename
        with meta_db._lock:
            meta_db.conn.execute("DELETE FROM songs WHERE filename = ?", (cache_key,))
            meta_db.conn.execute("DELETE FROM favorites WHERE filename = ?", (cache_key,))
            meta_db.conn.execute("DELETE FROM loops WHERE filename = ?", (cache_key,))
            meta_db.conn.commit()

        _invalidate_song_caches(cache_key)

    log.info("Deleted song %s", cache_key)
    # If a scan was mid-flight when we removed the row, it may already have
    # listed (and not yet processed) the file and will call ``meta_db.put()``
    # for it after our DB delete — reinserting a ghost row. Coalesce a
    # follow-up pass via ``_kick_scan`` so the next scan's ``delete_missing()``
    # purges that entry. Cheap no-op when no scan is running.
    if _scan_status["running"]:
        _kick_scan()
    return {"ok": True, "filename": cache_key}


# ── Library API ───────────────────────────────────────────────────────────────

def _split_csv(raw: str) -> list[str]:
    """Parse a comma-separated query-string list. Empty / whitespace-only
    entries are dropped so `arrangements_has=` (no value) and
    `arrangements_has=,` both mean 'no filter'."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _parse_has_lyrics(raw: str) -> int | None:
    """Tri-state parse for has_lyrics. `1` → require, `0` → exclude,
    anything else (including empty) → no filter."""
    if raw == "1":
        return 1
    if raw == "0":
        return 0
    return None


@app.get("/api/library")
def list_library(q: str = "", page: int = 0, size: int = 24, sort: str = "artist",
                 dir: str = "asc", favorites: int = 0, format: str = "",
                 arrangements_has: str = "", arrangements_lacks: str = "",
                 stems_has: str = "", stems_lacks: str = "",
                 has_lyrics: str = "", tunings: str = ""):
    """Paginated library search, queried from SQLite."""
    size = min(size, 100)
    fmt = format if format in ("psarc", "sloppak", "loose") else ""
    songs, total = meta_db.query_page(
        q=q, page=page, size=size, sort=sort,
        direction=dir, favorites_only=bool(favorites), format_filter=fmt,
        arrangements_has=_split_csv(arrangements_has),
        arrangements_lacks=_split_csv(arrangements_lacks),
        stems_has=_split_csv(stems_has),
        stems_lacks=_split_csv(stems_lacks),
        has_lyrics=_parse_has_lyrics(has_lyrics),
        tunings=_split_csv(tunings),
    )
    return {"songs": songs, "total": total, "page": page, "size": size}


@app.get("/api/library/artists")
def list_artists(letter: str = "", q: str = "", favorites: int = 0, page: int = 0, size: int = 50,
                 format: str = "",
                 arrangements_has: str = "", arrangements_lacks: str = "",
                 stems_has: str = "", stems_lacks: str = "",
                 has_lyrics: str = "", tunings: str = ""):
    """Get artists grouped by letter with albums and songs (for tree view)."""
    fmt = format if format in ("psarc", "sloppak", "loose") else ""
    artists, total = meta_db.query_artists(
        letter=letter, q=q, favorites_only=bool(favorites),
        page=page, size=min(size, 100), format_filter=fmt,
        arrangements_has=_split_csv(arrangements_has),
        arrangements_lacks=_split_csv(arrangements_lacks),
        stems_has=_split_csv(stems_has),
        stems_lacks=_split_csv(stems_lacks),
        has_lyrics=_parse_has_lyrics(has_lyrics),
        tunings=_split_csv(tunings),
    )
    return {"artists": artists, "total_artists": total, "page": page, "size": size}


@app.get("/api/library/stats")
def library_stats(favorites: int = 0, q: str = "", format: str = "",
                  arrangements_has: str = "", arrangements_lacks: str = "",
                  stems_has: str = "", stems_lacks: str = "",
                  has_lyrics: str = "", tunings: str = ""):
    """Aggregate stats for the UI. Accepts the same filter params as
    /api/library so the letter bar mirrors the active grid filter set."""
    fmt = format if format in ("psarc", "sloppak", "loose") else ""
    return meta_db.query_stats(
        favorites_only=bool(favorites), q=q, format_filter=fmt,
        arrangements_has=_split_csv(arrangements_has),
        arrangements_lacks=_split_csv(arrangements_lacks),
        stems_has=_split_csv(stems_has),
        stems_lacks=_split_csv(stems_lacks),
        has_lyrics=_parse_has_lyrics(has_lyrics),
        tunings=_split_csv(tunings),
    )


@app.get("/api/library/tuning-names")
def list_tuning_names():
    """Distinct tuning names present in the library, with per-tuning
    counts. Powers the tuning multi-select. Sorted by `tuning_sort_key`
    so names appear in the same musical order the sort uses
    (slopsmith#22) — E Standard first, then nearest neighbors."""
    rows = meta_db.conn.execute(
        "SELECT tuning_name, MIN(tuning_sort_key), COUNT(*) "
        # NULL/empty-name rows are excluded entirely from the picker —
        # users can't usefully filter by an unknown tuning. Once they
        # rescan, the rows acquire a name and join the list.
        "FROM songs WHERE title != '' AND COALESCE(tuning_name, '') != '' "
        "GROUP BY tuning_name COLLATE NOCASE "
        # Same ordering as `sort=tuning` in `query_page`: distance
        # from E Standard first, then signed-key ASC so the down-
        # tuned variant precedes the up-tuned one at equal distance
        # (Eb Standard before F Standard at distance 6). Final
        # alphabetical tiebreak keeps the order deterministic.
        # COALESCE around the sort_key guards against NULL the same
        # way the main tuning sort does.
        "ORDER BY ABS(COALESCE(MIN(tuning_sort_key), 0)), "
        "COALESCE(MIN(tuning_sort_key), 0) ASC, "
        "tuning_name COLLATE NOCASE"
    ).fetchall()
    return {
        "tunings": [
            {"name": name, "sort_key": int(sk or 0), "count": count}
            for name, sk, count in rows
        ],
    }


@app.post("/api/favorites/toggle")
def toggle_favorite(data: dict):
    """Toggle a song's favorite status."""
    filename = data.get("filename", "")
    if not filename:
        return {"error": "No filename"}
    new_state = meta_db.toggle_favorite(filename)
    return {"favorite": new_state}


# ── Loops API ────────────────────────────────────────────────────────────────

@app.get("/api/loops")
def list_loops(filename: str):
    rows = meta_db.conn.execute(
        "SELECT id, name, start_time, end_time FROM loops WHERE filename = ? ORDER BY start_time",
        (filename,)
    ).fetchall()
    return [{"id": r[0], "name": r[1], "start": r[2], "end": r[3]} for r in rows]


@app.post("/api/loops")
def save_loop(data: dict):
    filename = data.get("filename", "")
    name = data.get("name", "").strip()
    start = data.get("start")
    end = data.get("end")
    if not filename or start is None or end is None:
        return {"error": "Missing fields"}
    if not name:
        count = meta_db.conn.execute(
            "SELECT COUNT(*) FROM loops WHERE filename = ?", (filename,)
        ).fetchone()[0]
        name = f"Loop {count + 1}"
    with meta_db._lock:
        meta_db.conn.execute(
            "INSERT INTO loops (filename, name, start_time, end_time) VALUES (?, ?, ?, ?)",
            (filename, name, float(start), float(end))
        )
        meta_db.conn.commit()
    return {"ok": True, "name": name}


@app.delete("/api/loops/{loop_id}")
def delete_loop(loop_id: int):
    with meta_db._lock:
        meta_db.conn.execute("DELETE FROM loops WHERE id = ?", (loop_id,))
        meta_db.conn.commit()
    return {"ok": True}


# ── Settings API ──────────────────────────────────────────────────────────────

def _default_settings():
    """Fallback settings returned when config.json is missing or
    unreadable. Also used to seed a fresh cfg on first-run POSTs so a
    single-key write (e.g. the difficulty slider) can't silently wipe
    defaults that subsequent GETs would have exposed."""
    # Same `_DLC_DIR_ENV` truthy check as `_get_dlc_dir`: an empty env
    # var collapses to `Path(".")` whose `.is_dir()` is True, so without
    # the explicit guard we'd surface `"."` to /api/settings — and any
    # partial-update POST would then persist that into config.json,
    # silently undoing the env-var fix on the next load.
    return {
        "dlc_dir": str(DLC_DIR) if (_DLC_DIR_ENV and DLC_DIR.is_dir()) else "",
        "psarc_platform": "both",
    }


def _load_config(config_file):
    """Read and parse config.json. Returns the parsed dict, or None if
    the file is missing, unreadable, invalid JSON, or parses to a
    non-dict (e.g. the file contains `[]` or `42`). Callers treat None
    as "fall back to defaults". Shared between GET and POST so both
    handle bad files the same way."""
    if not config_file.exists():
        return None
    try:
        parsed = json.loads(config_file.read_text())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


@app.get("/api/settings")
def get_settings():
    cfg = _load_config(CONFIG_DIR / "config.json")
    return cfg if cfg is not None else _default_settings()


@app.post("/api/settings")
def save_settings(data: dict):
    # Partial-update: merge only keys present in the request body so
    # single-key POSTs (like the difficulty slider's oninput) don't
    # clobber unrelated settings on disk.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_file = CONFIG_DIR / "config.json"
    # Seed defaults when config.json is missing, unreadable, or parses
    # to a non-dict (e.g. `[]`, `42`). Without the non-dict guard, the
    # next `cfg["..."] = ...` assignment would raise TypeError and 500
    # the public endpoint. Seeding also ensures single-key POSTs (the
    # difficulty slider's fire-and-forget write) don't produce a config
    # file missing the dlc_dir fallback GET would have surfaced.
    cfg = _load_config(config_file)
    if cfg is None:
        cfg = _default_settings()

    messages = []
    if "dlc_dir" in data:
        dlc_path = data["dlc_dir"]
        # null / missing is no-op (preserve on-disk value). Only an
        # explicit empty string means "clear". Non-string values are
        # rejected so Path(...) can't be surprised by non-str JSON.
        if dlc_path is None:
            pass
        elif not isinstance(dlc_path, str):
            return {"error": "dlc_dir must be a string path or empty"}
        elif dlc_path == "":
            cfg["dlc_dir"] = ""
        else:
            if Path(dlc_path).is_dir():
                cfg["dlc_dir"] = dlc_path
                count = sum(1 for f in Path(dlc_path).iterdir() if f.suffix == ".psarc")
                messages.append(f"DLC folder: {count} .psarc files found")
            else:
                return {"error": f"DLC directory not found: {dlc_path}"}

    # Both of these are consumed downstream as strings (e.g.
    # demucs_server_url.rstrip('/') in lib/sloppak_convert.py), so
    # reject non-string shapes here. Matches the dlc_dir pattern above:
    # null is no-op, empty string clears, non-string is a structured
    # error that preserves the on-disk value.
    for key in ("default_arrangement", "demucs_server_url"):
        if key in data:
            raw = data[key]
            if raw is None:
                pass
            elif not isinstance(raw, str):
                return {"error": f"{key} must be a string or empty"}
            else:
                cfg[key] = raw
    if "master_difficulty" in data:
        # Coerce defensively — public endpoint, so `null`, `""`, or a
        # non-numeric string shouldn't 500 the request. float() accepts
        # both integer and float-shaped strings; anything else returns
        # a structured error like the dlc_dir branch above.
        raw = data["master_difficulty"]
        # Reject bool explicitly: Python makes bool a subclass of int, so
        # True/False would otherwise coerce to 1/0 and persist as a valid
        # difficulty. Caller almost certainly means "bad input".
        if isinstance(raw, bool):
            return {"error": "master_difficulty must be a number between 0 and 100"}
        try:
            cfg["master_difficulty"] = max(0, min(100, int(float(raw))))
        except (TypeError, ValueError, OverflowError):
            # OverflowError covers int(float("inf")) / int(float("1e309"))
            # which Python raises distinctly from ValueError.
            return {"error": "master_difficulty must be a number between 0 and 100"}

    if "av_offset_ms" in data:
        # Audio-output pipeline latency compensation. Positive values
        # mean audio is running ahead of visuals; the highway adds
        # this to its render clock to catch the visuals up. Clamped
        # to ±1000 ms to mirror the client-side slider — a direct
        # POST shouldn't be able to persist `1e9`. Same defensive
        # coercion shape as master_difficulty above (reject bool,
        # cover OverflowError, structured 4xx-style return on bad
        # input rather than 500).
        raw = data["av_offset_ms"]
        if isinstance(raw, bool):
            return {"error": "av_offset_ms must be a number between -1000 and 1000"}
        try:
            cfg["av_offset_ms"] = max(-1000.0, min(1000.0, float(raw)))
        except (TypeError, ValueError, OverflowError):
            return {"error": "av_offset_ms must be a number between -1000 and 1000"}

    if "psarc_platform" in data:
        raw = data["psarc_platform"]
        # null is a no-op (preserves on-disk value), matching the
        # dlc_dir / default_arrangement contract. Non-string and
        # out-of-range strings are rejected with a structured error.
        if raw is not None:
            if not isinstance(raw, str) or raw not in ("both", "pc", "mac"):
                return {"error": "psarc_platform must be 'both', 'pc', or 'mac'"}
            cfg["psarc_platform"] = raw

    config_file.write_text(json.dumps(cfg, indent=2))
    return {"message": ". ".join(messages) if messages else "Settings saved"}


# ── Settings export/import (slopsmith#113) ───────────────────────────────────

# Bumped only when the bundle JSON shape changes incompatibly. Importer
# refuses anything but this exact value — version mismatches are warned
# but not blocked, schema mismatches ARE blocked.
SETTINGS_BUNDLE_SCHEMA = 1


def _running_version() -> str:
    """Same lookup chain `/api/version` uses, factored out so the export
    bundle records what shipped this file. Kept as a helper so future
    changes (e.g. baked-in version) only have to touch one site."""
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    version_file = Path(__file__).parent / "VERSION"
    if version_file.exists():
        try:
            return version_file.read_text().strip()
        except (OSError, UnicodeDecodeError):
            pass
    return "unknown"


def _validate_server_config_types(cfg: dict) -> str | None:
    """Type-and-range gate for the server_config block of an import
    bundle, mirroring the per-key checks in `POST /api/settings`. The
    importer writes config.json verbatim, so without this gate a
    hand-edited bundle could persist a non-string `demucs_server_url`
    (which downstream code calls `.rstrip('/')` on and crashes) or an
    out-of-range `master_difficulty` (which bypasses the slider's
    clamp). Returns None on success, an error string on the first
    violation. Filesystem-existence checks (e.g. dlc_dir is_dir) are
    NOT performed here — restoring a bundle on a different machine
    legitimately may reference paths that don't exist locally yet,
    and the `POST /api/settings` interactive endpoint is the right
    place for that ergonomic check, not the bulk-restore path.
    Unknown keys are passed through so future settings (and per-plugin
    keys that may be added later) round-trip without code changes
    here."""
    if "dlc_dir" in cfg:
        v = cfg["dlc_dir"]
        if v is not None and not isinstance(v, str):
            return "server_config.dlc_dir must be a string"
    for key in ("default_arrangement", "demucs_server_url"):
        if key in cfg:
            v = cfg[key]
            if v is not None and not isinstance(v, str):
                return f"server_config.{key} must be a string"
    if "master_difficulty" in cfg:
        v = cfg["master_difficulty"]
        # bool is an int subclass — reject explicitly so True/False
        # don't quietly persist as 1/0 difficulty values.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "server_config.master_difficulty must be a number between 0 and 100"
        if not (0 <= v <= 100):
            return "server_config.master_difficulty must be between 0 and 100"
    if "av_offset_ms" in cfg:
        v = cfg["av_offset_ms"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "server_config.av_offset_ms must be a number between -1000 and 1000"
        if not (-1000 <= v <= 1000):
            return "server_config.av_offset_ms must be between -1000 and 1000"
    if "psarc_platform" in cfg:
        v = cfg["psarc_platform"]
        if v is not None and v not in ("both", "pc", "mac"):
            return "server_config.psarc_platform must be 'both', 'pc', or 'mac'"
    return None


class _UndeclaredFile(ValueError):
    """Raised when a relpath would otherwise be safe but isn't covered by
    the plugin's manifest allowlist. Distinct from the generic
    `ValueError` so the import handler can warn-and-skip this case
    without resorting to message-string matching (which would silently
    change behavior on a future error-text refactor)."""


def _matches_allowlist(relpath: str, allowed: list[str]) -> bool:
    """Return True if `relpath` is covered by an entry in the manifest's
    `_export_paths`. Entries ending in `/` are directory rules
    (strict prefix-match); other entries are exact-file rules. Both
    `relpath` and `allowed` are POSIX strings already normalized
    through `_normalize_export_paths` on the loader side. Caller is
    expected to pass an already-normalized relpath — `_validate_relpath`
    enforces this so a bundle can't satisfy a prefix rule with a
    string that later normalizes to a different target."""
    for allow in allowed:
        if allow.endswith("/"):
            # Strict prefix match only. We deliberately reject
            # `relpath == prefix.rstrip("/")` — a directory entry
            # never authorizes writing AT the directory itself, and
            # accepting that would let phase 2 try to `os.replace()`
            # over an existing directory and crash mid-apply.
            if relpath.startswith(allow):
                return True
        elif relpath == allow:
            return True
    return False


def _validate_relpath(relpath: str, allowed: list[str], config_dir: Path) -> Path:
    """Resolve `relpath` to an absolute path under `config_dir`, raising
    on anything that smells like path-traversal, an absolute path, or
    a manifest-undeclared file. Layered defenses:

      1. String-level: reject backslash, drive letter, absolute, and
         any `.` / `..` segment in the *raw* input — BEFORE any
         normalization. Critically, this catches the
         `allowed_dir/../config.json` shape: the raw string starts
         with `allowed_dir/`, so a naive prefix-match would accept
         it; if we then normalized first, the `..` would collapse
         away and the segment guard would have nothing to reject. By
         refusing pre-normalization any input containing a `.` or
         `..` segment, we make it impossible for a normalize-then-
         resolve pass to "launder" a hostile prefix into a different
         target.
      2. Allowlist match against the now-known-clean relpath.
         Allowlist-miss raises `_UndeclaredFile` (a `ValueError`
         subclass) so the caller can distinguish "manifest changed
         between export and import" from "this looks like an attack"
         without string-matching the error message.
      3. Realpath check: after resolving under config_dir, the target
         must still live inside config_dir. This catches symlinks-
         under-config_dir attacks where someone planted a symlink
         pointing out and tried to import a file "under" it.
      4. Symlink rejection: even when a symlink (or symlinked
         directory component) resolves to a path that *still* lives
         inside config_dir, importing through it would let an
         allowlisted relpath redirect the write to a different
         in-config file — bypassing the manifest's intent. We probe
         every path component from `config_dir` down to the target
         using `lstat`, refusing if any link is set on the chain.
         This matches the documented "symlinks are never followed on
         import" guarantee.

    Returns the resolved absolute path (caller writes there in phase 2).
    """
    if not isinstance(relpath, str) or not relpath or relpath != relpath.strip():
        raise ValueError(f"illegal relpath: {relpath!r}")
    # Reject backslashes outright — manifest entries are POSIX, and
    # accepting `foo\bar` here on a platform whose Path treats `\` as
    # a separator would let a hostile bundle smuggle traversal past
    # the part-by-part check below.
    if "\\" in relpath:
        raise ValueError(f"relpath uses non-POSIX separator: {relpath!r}")
    # Absolute / drive-letter check before splitting.
    if relpath.startswith("/") or (len(relpath) >= 2 and relpath[1] == ":"):
        raise ValueError(f"relpath must be relative: {relpath!r}")
    raw_parts = relpath.split("/")
    # Empty parts catch `foo//bar` and a trailing `/`. `.` / `..` catch
    # both leading and embedded forms (`./x`, `a/./b`, `allow/../escape`).
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"relpath contains illegal segment: {relpath!r}")
    # Defense-in-depth: any leading `.` segment (e.g. dotfile-disguised
    # paths like `.git/config`) is also rejected — config_dir isn't a
    # place plugins should be writing dotfiles, and accepting them here
    # would let one plugin claim a global filename like `.npmrc`.
    if raw_parts[0].startswith("."):
        raise ValueError(f"relpath starts with dotfile segment: {relpath!r}")

    if not _matches_allowlist(relpath, allowed):
        raise _UndeclaredFile(
            f"relpath not declared in plugin manifest: {relpath!r}"
        )

    target = (config_dir / relpath).resolve()
    config_root = config_dir.resolve()
    # `target == config_root` would mean the relpath resolved to the
    # config dir itself, which can't be a file write target — reject.
    if target == config_root:
        raise ValueError(f"relpath resolves to config_dir itself: {relpath!r}")
    if config_root not in target.parents:
        raise ValueError(f"relpath escapes config_dir: {relpath!r}")

    # Walk every component from config_dir down to (but not including)
    # the target file, refusing if any is a symlink. The target itself
    # is checked too — a symlinked file inside config_dir could still
    # redirect the write to another in-config file, defeating the
    # manifest's allowlist intent. `lstat` is the right primitive: it
    # reports the link itself rather than the link's destination, so a
    # broken or self-referential symlink won't slip through. Missing
    # intermediate dirs are fine — `_atomic_write_file` mkdirs them
    # under config_dir, and a path that doesn't exist yet trivially
    # isn't a symlink.
    probe = config_dir
    for part in relpath.split("/"):
        probe = probe / part
        try:
            st = os.lstat(probe)
        except FileNotFoundError:
            # Component doesn't exist yet → can't be a symlink. Any
            # remaining components also don't exist, so we're done.
            break
        import stat as _stat
        if _stat.S_ISLNK(st.st_mode):
            raise ValueError(
                f"relpath traverses or targets a symlink: {relpath!r}"
            )
    return target


def _encode_file(abs_path: Path) -> dict:
    """Encode a single file for the export bundle. JSON files that parse
    cleanly use the `json` encoding so the bundle stays diff-friendly;
    everything else (sqlite, NAM models, IRs, binary blobs) falls back
    to base64. Symlinks are skipped at the caller — we never reach this
    helper for them."""
    import base64
    raw = abs_path.read_bytes()
    if abs_path.suffix.lower() == ".json":
        try:
            return {"encoding": "json", "data": json.loads(raw.decode("utf-8"))}
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Fall through to base64 — file claimed `.json` but isn't
            # valid JSON; preserve bytes verbatim rather than refusing.
            pass
    return {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}


def _decode_entry(entry: dict) -> bytes:
    """Inverse of `_encode_file`. Raises ValueError on malformed entries
    so phase 1 of the importer can refuse the whole bundle without
    having written anything."""
    import base64
    if not isinstance(entry, dict):
        raise ValueError(f"file entry must be an object, got {type(entry).__name__}")
    encoding = entry.get("encoding")
    data = entry.get("data")
    if encoding == "base64":
        if not isinstance(data, str):
            raise ValueError("base64 entry: 'data' must be a string")
        try:
            return base64.b64decode(data, validate=True)
        except Exception as e:
            raise ValueError(f"base64 entry: invalid payload ({e})")
    if encoding == "json":
        # We re-serialize the parsed value with stable formatting. Round
        # trips with the original byte stream aren't guaranteed (key
        # order, whitespace), but the file's *meaning* is preserved.
        try:
            return json.dumps(data, indent=2).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise ValueError(f"json entry: cannot re-serialize ({e})")
    raise ValueError(f"unknown encoding: {encoding!r}")


def _walk_export_paths(allowed: list[str], config_dir: Path) -> dict:
    """Expand a plugin's `_export_paths` against disk and return a
    `{relpath: encoded_entry}` dict. Missing files are silently skipped
    (intentional — manifests can list optional files). Symlinks are
    skipped with no entry. Directories are walked recursively; their
    contained files surface as POSIX-joined relpaths.

    Symlink policy is "skipped and never followed" at every depth:
    `os.walk(..., followlinks=False)` ensures we don't *recurse* into
    symlinked subdirectories, but we additionally drop any symlinked
    entry from `dirnames` (so its name isn't even reported to the
    caller, even though the walker wouldn't descend) and skip files
    whose path is itself a symlink. Without those extra filters, a
    planted symlink directory under an allowed prefix could leak data
    from outside `config_dir` into the export bundle.
    """
    out: dict[str, dict] = {}
    for entry in allowed:
        is_dir = entry.endswith("/")
        rel = entry.rstrip("/")
        abs_target = config_dir / rel
        if abs_target.is_symlink():
            continue
        if is_dir:
            if not abs_target.is_dir():
                continue
            collected: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(
                str(abs_target), followlinks=False
            ):
                # Strip symlinked subdirs from `dirnames` in-place so
                # the walker neither yields their names nor descends.
                dirnames[:] = [
                    d for d in dirnames
                    if not os.path.islink(os.path.join(dirpath, d))
                ]
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    collected.append(Path(full))
            # Sort for deterministic bundle output (test fixtures and
            # diffs both rely on stable ordering).
            for child in sorted(collected):
                # POSIX-joined relpath relative to config_dir keeps the
                # bundle cross-platform — Windows-authored bundles can
                # be applied on Linux and vice versa.
                child_rel = child.relative_to(config_dir).as_posix()
                out[child_rel] = _encode_file(child)
        else:
            if not abs_target.is_file():
                continue
            out[rel] = _encode_file(abs_target)
    return out


def _atomic_write_file(target: Path, payload: bytes):
    """Write `payload` to `target` via a uniquely-named sibling temp file
    + os.replace. `os.replace` is atomic on both POSIX and Win32 —
    readers see either the old file or the new one, never a half-written
    state.

    The temp name is generated by `tempfile.mkstemp` so two concurrent
    imports (or two workers sharing the same config volume) can't race
    on the same `<target>.tmp.import` path and clobber each other's
    in-flight writes. On any failure between mkstemp and the successful
    `os.replace`, we remove the temp file so a failed import doesn't
    leave `.tmp.import` litter under config_dir."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp.import",
    )
    tmp = Path(tmp_name)
    # Hand fd to os.fdopen inside its own try, so a failure to wrap
    # the descriptor (rare — typically EMFILE / ENOMEM) doesn't leak
    # the raw fd. On Windows an open fd would also keep the temp file
    # locked and undeletable. Once `with` enters, the fdopen'd file
    # owns close responsibility.
    try:
        f = os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        with f:
            f.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@app.get("/api/settings/export")
def export_settings():
    """Build a settings bundle covering server config + opted-in plugin
    server-side files. Frontend layers in `local_storage` before
    triggering the download. See slopsmith#113."""
    import datetime
    from plugins import LOADED_PLUGINS, PLUGINS_LOCK

    config_file = CONFIG_DIR / "config.json"
    server_config = _load_config(config_file)
    if server_config is None:
        server_config = _default_settings()

    plugin_blocks: dict[str, dict] = {}
    with PLUGINS_LOCK:
        plugins_snapshot = list(LOADED_PLUGINS)
    for p in plugins_snapshot:
        allowed = p.get("_export_paths") or []
        plugin_blocks[p["id"]] = {"files": _walk_export_paths(allowed, CONFIG_DIR)}

    # Capture the timestamp once so the bundle's `exported_at` and the
    # download filename's date prefix can't disagree if the request
    # crosses midnight UTC between the two formats.
    now = datetime.datetime.now(datetime.timezone.utc)
    bundle = {
        "schema": SETTINGS_BUNDLE_SCHEMA,
        "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slopsmith_version": _running_version(),
        "server_config": server_config,
        "plugin_server_configs": plugin_blocks,
    }
    filename = f"slopsmith-settings-{now.strftime('%Y-%m-%d')}.json"
    return JSONResponse(
        bundle,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/settings/import")
def import_settings(bundle: dict):
    """Apply a previously exported settings bundle. Validates the entire
    bundle in phase 1 (no disk writes); only on full success does
    phase 2 commit each file via temp+rename. The frontend reads
    `local_storage` itself — server ignores it. See slopsmith#113."""
    from plugins import LOADED_PLUGINS, PLUGINS_LOCK

    if not isinstance(bundle, dict):
        return JSONResponse({"ok": False, "error": "bundle must be a JSON object"}, status_code=400)

    # ── Phase 1: validate everything before touching disk ────────────
    schema = bundle.get("schema")
    if schema != SETTINGS_BUNDLE_SCHEMA:
        return JSONResponse(
            {
                "ok": False,
                "error": f"unsupported schema {schema!r}; this server speaks schema {SETTINGS_BUNDLE_SCHEMA}",
            },
            status_code=400,
        )

    server_config = bundle.get("server_config")
    if not isinstance(server_config, dict):
        return JSONResponse(
            {"ok": False, "error": "server_config must be an object"},
            status_code=400,
        )
    cfg_err = _validate_server_config_types(server_config)
    if cfg_err is not None:
        return JSONResponse(
            {"ok": False, "error": cfg_err},
            status_code=400,
        )

    plugin_blocks = bundle.get("plugin_server_configs") or {}
    if not isinstance(plugin_blocks, dict):
        return JSONResponse(
            {"ok": False, "error": "plugin_server_configs must be an object"},
            status_code=400,
        )

    warnings: list[str] = []
    bundle_version = bundle.get("slopsmith_version")
    running = _running_version()
    if bundle_version and bundle_version != running:
        warnings.append(
            f"version mismatch: bundle {bundle_version!r} vs running {running!r}; importing anyway"
        )

    with PLUGINS_LOCK:
        by_id = {p["id"]: p for p in LOADED_PLUGINS}

    # Stage every (display_relpath, target_abs_path, payload) tuple before
    # writing. The relpath is what we surface in the `partial` field on a
    # mid-apply failure — absolute paths would leak the deployment's
    # config_dir layout, while the relpath is the same identifier the
    # bundle itself used and is portable across machines.
    staged: list[tuple[str, Path, bytes]] = []
    applied_plugins: list[str] = []
    for plugin_id, block in plugin_blocks.items():
        if not isinstance(plugin_id, str) or not plugin_id:
            return JSONResponse(
                {"ok": False, "error": f"invalid plugin id key: {plugin_id!r}"},
                status_code=400,
            )
        plugin = by_id.get(plugin_id)
        if plugin is None:
            warnings.append(f"plugin {plugin_id!r} not loaded; skipping its files")
            continue
        if not isinstance(block, dict):
            return JSONResponse(
                {"ok": False, "error": f"plugin {plugin_id!r}: block must be an object"},
                status_code=400,
            )
        files = block.get("files") or {}
        if not isinstance(files, dict):
            return JSONResponse(
                {"ok": False, "error": f"plugin {plugin_id!r}: files must be an object"},
                status_code=400,
            )
        allowed = plugin.get("_export_paths") or []
        skipped_for_plugin: list[str] = []
        applied_for_plugin = False
        for relpath, file_entry in files.items():
            try:
                target = _validate_relpath(relpath, allowed, CONFIG_DIR)
            except _UndeclaredFile:
                # Manifest-allowlist miss is a normal outcome of a
                # plugin update between export and import — warn-and-
                # skip so the rest of the bundle still applies.
                skipped_for_plugin.append(relpath)
                continue
            except ValueError as e:
                # Path-traversal / absolute-path / illegal-segment /
                # backslash / dotfile errors are hard failures: we
                # never want to apply a bundle that contains those,
                # even partially. Caught AFTER `_UndeclaredFile`
                # because that's a `ValueError` subclass — Python
                # would otherwise route it through this branch.
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"plugin {plugin_id!r}, file {relpath!r}: {e}",
                    },
                    status_code=400,
                )
            try:
                payload = _decode_entry(file_entry)
            except ValueError as e:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"plugin {plugin_id!r}, file {relpath!r}: {e}",
                    },
                    status_code=400,
                )
            # Display key prefixes the plugin id so a partial-failure
            # report is unambiguous when two plugins happen to declare
            # files with the same relpath.
            display = f"{plugin_id}/{relpath}"
            staged.append((display, target, payload))
            applied_for_plugin = True
        if skipped_for_plugin:
            warnings.append(
                f"plugin {plugin_id!r}: skipped {len(skipped_for_plugin)} file(s) "
                f"no longer declared in manifest: {skipped_for_plugin}"
            )
        if applied_for_plugin:
            applied_plugins.append(plugin_id)

    # ── Phase 2: commit ──────────────────────────────────────────────
    written: list[str] = []
    try:
        for display, target, payload in staged:
            _atomic_write_file(target, payload)
            written.append(display)
        # Server config last so a write failure on a plugin file
        # doesn't leave config.json mismatched against the (untouched)
        # plugin state. Full-replace: caller is responsible for the
        # whole dict — this is restore semantics, not partial-update.
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_file(
            CONFIG_DIR / "config.json",
            json.dumps(server_config, indent=2).encode("utf-8"),
        )
    except OSError as e:
        # Phase-1 validation should have caught all foreseeable
        # failures; an OSError here means disk-level trouble (ENOSPC,
        # permission). We can't roll back already-replaced files
        # because we didn't snapshot them — surface what got written
        # (as relpaths, not absolute server paths) so the user knows
        # the state is partial without leaking deployment layout.
        return JSONResponse(
            {
                "ok": False,
                "error": f"write failed mid-apply: {e}",
                "partial": written,
            },
            status_code=500,
        )

    return {
        "ok": True,
        "warnings": warnings,
        "applied": {
            "server_config": True,
            "plugins": applied_plugins,
        },
    }


# ── Diagnostic bundle export (slopsmith#166) ──────────────────────────
#
# One-click "Export Diagnostics" in Settings produces a redacted zip
# combining server logs, system info, hardware (CPU/GPU/RAM), plugin
# inventory, and the browser-side console transcript + hardware probe.
# The bundle format is specified in docs/diagnostics-bundle-spec.md.

from fastapi import Body
from fastapi.responses import Response

from diagnostics_bundle import build_bundle as _diag_build, preview_bundle as _diag_preview
from diagnostics_hardware import collect as _diag_hardware


def _diag_log_file() -> Path | None:
    raw = os.environ.get("LOG_FILE", "").strip()
    if not raw:
        return None
    return Path(raw)


def _diag_plugins_roots() -> list[Path]:
    """Return all plugin root directories for orphan scanning.

    Includes both the built-in ``plugins/`` directory and
    ``SLOPSMITH_PLUGINS_DIR`` when set, so user-installed plugins and
    orphans in the external dir are reflected in the bundle.
    """
    roots: list[Path] = []
    user_dir = os.environ.get("SLOPSMITH_PLUGINS_DIR", "").strip()
    if user_dir:
        p = Path(user_dir)
        if p.is_dir():
            roots.append(p)
    builtin = Path(__file__).parent / "plugins"
    if builtin not in roots:
        roots.append(builtin)
    return roots


def _diag_coerce_bool(v, *, default: bool = True) -> bool:
    """Coerce a request-side value to bool, accepting both JSON booleans and
    string representations.

    - Falsy strings: ``"false"``, ``"0"``, ``"no"``, ``""`` → ``False``
    - ``None`` → *default*
    - Everything else (including ``"true"``, ``"1"``) → ``True``
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "")
    return bool(v)


def _diag_normalize_include(include: dict | None) -> dict:
    """Coerce request-side flags to the booleans build_bundle expects.
    Missing keys default to True so a bare {} request still produces
    the full bundle.

    Accepts both JSON booleans (``true``/``false``) and string
    representations so callers that serialize flags as strings behave
    consistently with the preview endpoint:
    - Falsy strings: ``"false"``, ``"0"``, ``"no"``, ``""`` → ``False``
    - Everything else (including ``"true"``, ``"1"``, ``"yes"``) → ``True``
    """
    keys = ("system", "hardware", "logs", "console", "plugins")
    if not isinstance(include, dict):
        return {k: True for k in keys}

    return {k: _diag_coerce_bool(include.get(k), default=True) for k in keys}


# Server-side caps on client-supplied payload sections.  diagnostics.js
# enforces a 500-entry / ~250 KB ring buffer on the browser side; these
# bounds give generous headroom while still preventing a crafted POST from
# forcing the server to allocate arbitrarily large in-memory bundles.
_DIAG_MAX_CONSOLE_ENTRIES = 1000          # hard cap: truncate silently
_DIAG_MAX_CONSOLE_BYTES = 2 * 1024 * 1024  # 2 MB hard cap on total console list
_DIAG_MAX_CLIENT_PAYLOAD_BYTES = 2 * 1024 * 1024   # 2 MB per dict section
_DIAG_MAX_CONTRIBUTIONS_BYTES = 4 * 1024 * 1024    # 4 MB aggregate cap for contributions


def _diag_cap_console(v) -> list | None:
    """Return *v* if it is a list, truncated to _DIAG_MAX_CONSOLE_ENTRIES entries
    and _DIAG_MAX_CONSOLE_BYTES total.  Entries are accumulated until either cap
    is reached; no partial-entry splitting occurs."""
    if not isinstance(v, list):
        return None
    result = v[:_DIAG_MAX_CONSOLE_ENTRIES]
    # Also enforce a byte cap — the count cap alone does not bound memory when
    # entries contain arbitrarily large strings.
    try:
        out = []
        total = 0
        for entry in result:
            encoded = json.dumps(entry, separators=(",", ":")).encode("utf-8", errors="replace")
            if total + len(encoded) > _DIAG_MAX_CONSOLE_BYTES:
                break
            out.append(entry)
            total += len(encoded)
        return out
    except (TypeError, ValueError):
        return None


def _diag_cap_dict(v) -> dict | None:
    """Return *v* if it is a dict whose JSON serialisation fits within
    _DIAG_MAX_CLIENT_PAYLOAD_BYTES, otherwise return None."""
    if not isinstance(v, dict):
        return None
    try:
        encoded = json.dumps(v, separators=(",", ":")).encode("utf-8", errors="replace")
    except (TypeError, ValueError) as e:
        log.warning("diagnostics client payload is not JSON-serialisable, dropping: %s", e)
        return None
    if len(encoded) > _DIAG_MAX_CLIENT_PAYLOAD_BYTES:
        return None
    return v


def _diag_cap_contributions(v, known_ids=None) -> dict | None:
    """Apply per-plugin and aggregate size caps on client_contributions.

    Unlike _diag_cap_dict(), which drops the whole dict when any plugin
    exceeds the limit, this function caps each plugin independently so
    one noisy plugin does not silence every other plugin's contribution.

    Parameters
    ----------
    v:
        The raw contributions dict from the POST payload.
    known_ids:
        When provided, contributions from plugins not in this set are
        skipped *before* serialisation, preventing a malicious caller
        from forcing the server to JSON-encode hundreds of near-limit
        payloads that ``build_bundle()`` would later discard anyway.
        ``None`` means "accept all plugin ids" (used in tests / preview).
    """
    if not isinstance(v, dict):
        return None
    result = {}
    total_bytes = 0
    for pid, contribution in v.items():
        if not isinstance(pid, str):
            continue
        # Filter unknown plugin ids early — before serialising — so a
        # crafted request cannot force large allocations for plugins that
        # build_bundle() would drop.
        if known_ids is not None and pid not in known_ids:
            continue
        try:
            encoded = json.dumps(contribution, separators=(",", ":")).encode("utf-8", errors="replace")
        except (TypeError, ValueError) as e:
            log.warning(
                "client_contributions[%r] is not JSON-serialisable, dropping: %s", pid, e
            )
            continue
        if len(encoded) > _DIAG_MAX_CLIENT_PAYLOAD_BYTES:
            log.warning(
                "client_contributions[%r] exceeds %d bytes, dropping",
                pid, _DIAG_MAX_CLIENT_PAYLOAD_BYTES,
            )
            continue
        if total_bytes + len(encoded) > _DIAG_MAX_CONTRIBUTIONS_BYTES:
            log.warning(
                "client_contributions aggregate size limit (%d bytes) reached, "
                "dropping remaining entries",
                _DIAG_MAX_CONTRIBUTIONS_BYTES,
            )
            break
        result[pid] = contribution
        total_bytes += len(encoded)
    return result or None


@app.post("/api/diagnostics/export")
def export_diagnostics(payload: dict = Body(default_factory=dict)):
    """Build a diagnostic bundle and stream it back as a zip download.

    The browser layers in `client_console`, `client_hardware`,
    `client_ua`, and `local_storage` before posting; the server adds
    server logs, hardware, plugin inventory, and packages everything
    into a single zip.

    Errors during plugin diagnostics callables are caught and logged
    to the bundle's manifest `notes` rather than failing the export.
    """
    from plugins import LOADED_PLUGINS, PLUGINS_LOCK

    redact = _diag_coerce_bool(payload.get("redact", True), default=True)
    include = _diag_normalize_include(payload.get("include"))
    client_console = _diag_cap_console(payload.get("client_console"))
    client_hardware = _diag_cap_dict(payload.get("client_hardware"))
    client_ua = _diag_cap_dict(payload.get("client_ua"))
    local_storage = _diag_cap_dict(payload.get("local_storage"))
    # Fetch the plugin list first so we can filter contributions to known
    # plugin ids before serialising — prevents a crafted request from
    # forcing large allocations for plugins build_bundle() would drop.
    with PLUGINS_LOCK:
        plugins_snapshot = list(LOADED_PLUGINS)
    known_ids = {p.get("id") for p in plugins_snapshot if isinstance(p.get("id"), str)}
    client_contributions = _diag_cap_contributions(
        payload.get("client_contributions"), known_ids=known_ids
    )

    zip_bytes, filename, _manifest = _diag_build(
        slopsmith_version=_running_version(),
        config_dir=CONFIG_DIR,
        dlc_dir=_get_dlc_dir(),
        log_file=_diag_log_file(),
        loaded_plugins=plugins_snapshot,
        include=include,
        redact=redact,
        client_console=client_console,
        client_hardware=client_hardware,
        client_ua=client_ua,
        local_storage=local_storage,
        client_contributions=client_contributions,
        log=log,
        plugins_root=_diag_plugins_roots(),
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/diagnostics/preview")
def preview_diagnostics(
    redact: bool = True,
    system: bool = True,
    hardware: bool = True,
    logs: bool = True,
    console: bool = True,
    plugins: bool = True,
):
    """Return what `/api/diagnostics/export` would produce, minus the
    actual file contents — file tree, sizes, schemas, redaction counts.
    Lets the Settings UI show the user what's about to be sent."""
    from plugins import LOADED_PLUGINS, PLUGINS_LOCK

    include = {
        "system": system,
        "hardware": hardware,
        "logs": logs,
        "console": console,
        "plugins": plugins,
    }
    with PLUGINS_LOCK:
        plugins_snapshot = list(LOADED_PLUGINS)
    return _diag_preview(
        slopsmith_version=_running_version(),
        config_dir=CONFIG_DIR,
        dlc_dir=_get_dlc_dir(),
        log_file=_diag_log_file(),
        loaded_plugins=plugins_snapshot,
        include=include,
        redact=redact,
        log=log,
        plugins_root=_diag_plugins_roots(),
    )


@app.get("/api/diagnostics/hardware")
def diagnostics_hardware():
    """Backend hardware probe (cross-platform). Reusable independently
    of the bundle export — handy for "what's my GPU" plugin queries."""
    return _diag_hardware()


# ── Plugin-provided routes are registered at startup via plugins/__init__.py ─
# (CustomsForge, Ultimate Guitar, etc. are loaded from plugins/ directory)



@app.websocket("/ws/retune")
async def ws_retune(websocket: WebSocket, filename: str, target: str = "E Standard"):
    """Retune a song to a target tuning with real-time progress."""
    import asyncio
    await websocket.accept()
    structlog.contextvars.bind_contextvars(ws_conn_id=uuid.uuid4().hex[:8])

    dlc = _get_dlc_dir()
    if not dlc:
        await websocket.send_json({"error": "DLC folder not configured"})
        await websocket.close()
        return

    psarc_path = _resolve_dlc_path(dlc, filename)
    if psarc_path is None:
        await websocket.send_json({"error": "forbidden"})
        await websocket.close()
        return
    if not psarc_path.exists():
        await websocket.send_json({"error": "File not found"})
        await websocket.close()
        return

    # Retune only operates on PSARC containers — sloppak is an open format
    # and doesn't share the SNG/encryption pipeline retune.py depends on.
    if filename.lower().endswith(".sloppak") or sloppak_mod.is_sloppak(psarc_path):
        await websocket.send_json({"error": "Retune is not supported for .sloppak files"})
        await websocket.close()
        return

    # Bounded queue: retune can emit many progress messages (one per WEM
    # file processed plus stage milestones), so 256 is a generous ceiling
    # even for large PSARC bundles.  When the consumer exits early
    # (client disconnect), put_nowait raises QueueFull — _queue_put_safe
    # catches it silently so the executor thread doesn't block or accumulate
    # memory waiting for a consumer that will never drain the queue.
    progress_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _queue_put_safe(item, terminal=False) -> None:
        try:
            progress_queue.put_nowait(item)
        except asyncio.QueueFull:
            if terminal:
                # Terminal done/error messages must reach the client.  Make
                # room by discarding the oldest intermediate progress update.
                try:
                    progress_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    progress_queue.put_nowait(item)
                except asyncio.QueueFull:
                    pass  # should not happen after making room
            # else: consumer gone (client disconnected); discard the update

    def _do_retune():
        from retune import retune_to_standard

        def report(stage, pct):
            loop.call_soon_threadsafe(_queue_put_safe, {"stage": stage, "progress": pct})

        try:
            # Only E Standard is supported; Drop D requires per-string pitch
            # shifting which retune_to_standard() does not implement.
            if target != "E Standard":
                loop.call_soon_threadsafe(
                    _queue_put_safe,
                    {"error": f"Unsupported target tuning: {target!r}. Only 'E Standard' is supported."},
                    True,
                )
                return

            report("Checking tuning...", 5)

            p = Path(psarc_path)
            stem = p.stem.replace("_p", "")
            out_path = str(p.parent / f"{stem}_EStd_p.psarc")
            result = retune_to_standard(str(psarc_path), output_path=out_path, on_progress=report)

            # Cache metadata for new file
            new_path = Path(result)
            if new_path.exists():
                try:
                    meta = _extract_meta_for_file(new_path)
                    stat = new_path.stat()
                    meta_db.put(new_path.name, stat.st_mtime, stat.st_size, meta)
                except Exception:
                    log.debug("retune: failed to cache metadata for %s", new_path.name, exc_info=True)

            loop.call_soon_threadsafe(_queue_put_safe, {
                "done": True, "progress": 100,
                "stage": "Complete!",
                "filename": new_path.name,
            }, True)

        except ValueError as e:
            log.warning("retune rejected for %s: %s", filename, e)
            loop.call_soon_threadsafe(_queue_put_safe, {"error": str(e)}, True)
        except Exception as e:
            log.exception("retune failed for %s", filename)
            loop.call_soon_threadsafe(_queue_put_safe, {"error": str(e)}, True)

    _ctx = contextvars.copy_context()
    build_task = loop.run_in_executor(None, lambda: _ctx.run(_do_retune))

    try:
        while True:
            try:
                msg = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                await websocket.send_json(msg)
                if msg.get("done") or msg.get("error"):
                    break
            except asyncio.TimeoutError:
                if build_task.done():
                    break
    except WebSocketDisconnect:
        pass

    await websocket.close()


@app.get("/api/song/{filename:path}/art")
async def get_song_art(filename: str):
    """Serve album art for a song.

    Dispatches by format and returns the appropriate media type:
      - Sloppak: serves `cover.jpg` (or manifest-declared cover) from
        the source dir as JPEG/PNG/WebP.
      - Loose folder: serves the discovered art file directly as
        JPEG/PNG/WebP.
      - PSARC: extracts and caches the embedded album art as PNG.
    """
    import asyncio
    dlc = _get_dlc_dir()
    if not dlc:
        return JSONResponse({"error": "not configured"}, 404)

    psarc_path = _resolve_dlc_path(dlc, filename)
    if psarc_path is None:
        return JSONResponse({"error": "forbidden"}, 403)
    if not psarc_path.exists():
        return JSONResponse({"error": "not found"}, 404)

    # Sloppak path: pull cover.jpg from the source dir (manifest-declared or default).
    if sloppak_mod.is_sloppak(psarc_path):
        try:
            src = sloppak_mod.resolve_source_dir(filename, dlc, SLOPPAK_CACHE_DIR)
            manifest = sloppak_mod.load_manifest(psarc_path)
            cover_rel = str(manifest.get("cover") or "cover.jpg")
            cover_path = (src / cover_rel).resolve()
            # Prevent escape and fall back to default name if missing.
            try:
                cover_path.relative_to(src.resolve())
            except ValueError:
                return JSONResponse({"error": "forbidden"}, 403)
            if cover_path.exists() and cover_path.is_file():
                mt = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".webp": "image/webp",
                }.get(cover_path.suffix.lower(), "image/jpeg")
                return FileResponse(str(cover_path), media_type=mt)
        except Exception:
            pass
        return JSONResponse({"error": "no art"}, 404)

    # Loose folder path: serve art file directly.
    # psarc_path is already validated against DLC_DIR by _resolve_dlc_path.
    if loosefolder_mod.is_loose_song(psarc_path):
        art_path = loosefolder_mod.find_art(psarc_path)
        if art_path:
            # Re-resolve in case the matched file is a symlink — a crafted
            # CDLC could put `album_art.jpg` as a symlink to anywhere on
            # disk. Insist the final target stays inside the song folder.
            art_resolved = art_path.resolve()
            try:
                art_resolved.relative_to(psarc_path)
            except ValueError:
                return JSONResponse({"error": "forbidden"}, 403)
            if art_resolved.is_file():
                mt = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".webp": "image/webp",
                }.get(art_resolved.suffix.lower(), "image/jpeg")
                return FileResponse(str(art_resolved), media_type=mt)
        return JSONResponse({"error": "no art"}, 404)

    # Check cache first
    art_cache = ART_CACHE_DIR
    art_cache.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace(" ", "_")
    cached = art_cache / f"{safe_name}.png"
    if cached.exists():
        return FileResponse(str(cached), media_type="image/png")

    def _extract_art():
        tmp = tempfile.mkdtemp(prefix="rs_art_")
        try:
            unpack_psarc(str(psarc_path), tmp)
            dds_files = sorted(Path(tmp).rglob("*.dds"), key=lambda p: p.stat().st_size, reverse=True)
            if not dds_files:
                return None
            from PIL import Image
            img = Image.open(dds_files[0]).convert("RGB")
            img.save(str(cached), "PNG")
            return str(cached)
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    result = await asyncio.get_event_loop().run_in_executor(None, _extract_art)
    if result:
        return FileResponse(result, media_type="image/png")
    return JSONResponse({"error": "no art"}, 404)


@app.post("/api/song/{filename:path}/meta")
def update_song_meta(filename: str, data: dict):
    """Update song metadata in the cache."""
    # Canonicalise to the same key get_song_info uses so an update via
    # one URL form (e.g. with `..` segments) lands on the row that
    # later reads will see.
    dlc = _get_dlc_dir()
    cache_key = filename
    if dlc:
        resolved = _resolve_dlc_path(dlc, filename)
        if resolved is None:
            return JSONResponse({"error": "forbidden"}, 403)
        try:
            cache_key = resolved.relative_to(dlc.resolve()).as_posix()
        except ValueError:
            pass
    with meta_db._lock:
        updates = []
        params = []
        for field in ("title", "artist", "album", "year"):
            if field in data:
                updates.append(f"{field} = ?")
                params.append(data[field])
        if not updates:
            return {"error": "No fields to update"}
        params.append(cache_key)
        meta_db.conn.execute(
            f"UPDATE songs SET {', '.join(updates)} WHERE filename = ?", params
        )
        meta_db.conn.commit()
    return {"ok": True}


@app.post("/api/song/{filename:path}/art/upload")
async def upload_song_art_b64(filename: str, data: dict):
    """Upload custom album art as base64 PNG/JPG."""
    import base64
    b64 = data.get("image", "")
    if not b64:
        return {"error": "No image data"}
    # Strip data URL prefix if present
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        img_data = base64.b64decode(b64)
    except Exception:
        return {"error": "Invalid base64"}

    art_cache = ART_CACHE_DIR
    art_cache.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace(" ", "_")
    cached = art_cache / f"{safe_name}.png"

    # Convert to PNG if needed
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_data)).convert("RGB")
        img.save(str(cached), "PNG")
    except Exception as e:
        return {"error": f"Invalid image: {e}"}

    return {"ok": True}


@app.get("/api/song/{filename:path}")
async def get_song_info(filename: str):
    """Return song metadata, from cache or by extracting PSARC."""
    import asyncio
    dlc = _get_dlc_dir()
    if not dlc:
        return JSONResponse({"error": "DLC folder not configured"}, 404)

    psarc_path = _resolve_dlc_path(dlc, filename)
    if psarc_path is None:
        return JSONResponse({"error": "forbidden"}, 403)
    if not psarc_path.exists():
        return JSONResponse({"error": "File not found"}, 404)

    # Canonicalise the cache key against the resolved path so two URL
    # forms of the same physical file (e.g. `Artist/song.psarc` vs
    # `Artist/../Artist/song.psarc`) converge on a single row instead
    # of fragmenting / shadowing each other in meta_db.
    try:
        cache_key = psarc_path.relative_to(dlc.resolve()).as_posix()
    except ValueError:
        cache_key = filename

    mtime, size = _stat_for_cache(psarc_path)
    cached = meta_db.get(cache_key, mtime, size)
    if cached:
        return cached

    # Extract in thread pool
    def _extract():
        meta = _extract_meta_for_file(psarc_path)
        meta_db.put(cache_key, mtime, size, meta)
        return meta

    meta = await asyncio.get_event_loop().run_in_executor(None, _extract)
    return meta


# ── Highway WebSocket ─────────────────────────────────────────────────────────

# Cache extracted PSARCs to avoid re-extraction on arrangement switch
_extract_cache = {}  # filename -> (tmp_dir, song, timestamp)
_extract_cache_lock = threading.Lock()


def _get_or_extract(filename, psarc_path):
    """Return cached extraction or extract fresh."""
    import time
    with _extract_cache_lock:
        cached = _extract_cache.get(filename)
        if cached:
            tmp, song, ts = cached
            if Path(tmp).exists() and (time.time() - ts) < 300:  # 5 min cache
                return tmp, song, False  # False = not new
            else:
                shutil.rmtree(tmp, ignore_errors=True)
                del _extract_cache[filename]

    tmp = tempfile.mkdtemp(prefix="rs_web_")
    unpack_psarc(str(psarc_path), tmp)
    song = load_song(tmp)

    with _extract_cache_lock:
        # Clean old entries if cache gets too big
        if len(_extract_cache) > 10:
            oldest = min(_extract_cache, key=lambda k: _extract_cache[k][2])
            old_tmp = _extract_cache.pop(oldest)[0]
            shutil.rmtree(old_tmp, ignore_errors=True)
        import time as _t
        _extract_cache[filename] = (tmp, song, _t.time())

    return tmp, song, True  # True = freshly extracted


@app.get("/api/sloppak/{filename:path}/file/{rel_path:path}")
def serve_sloppak_file(filename: str, rel_path: str):
    """Serve a file from inside a sloppak (stems, cover, etc.)."""
    src = sloppak_mod.get_cached_source_dir(filename)
    if src is None:
        dlc = _get_dlc_dir()
        if not dlc:
            return JSONResponse({"error": "not configured"}, 404)
        try:
            src = sloppak_mod.resolve_source_dir(filename, dlc, SLOPPAK_CACHE_DIR)
        except Exception:
            return JSONResponse({"error": "not found"}, 404)
    # Prevent path traversal.
    target = (src / rel_path).resolve()
    try:
        target.relative_to(src.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, 403)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, 404)
    ext = target.suffix.lower()
    mt = {
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".oga": "audio/ogg",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".json": "application/json",
    }.get(ext)
    return FileResponse(str(target), media_type=mt) if mt else FileResponse(str(target))


@app.websocket("/ws/highway/{filename:path}")
async def highway_ws(websocket: WebSocket, filename: str, arrangement: int = -1):
    """Stream song data for the highway renderer over WebSocket."""
    await websocket.accept()
    structlog.contextvars.bind_contextvars(ws_conn_id=uuid.uuid4().hex[:8])

    dlc = _get_dlc_dir()
    if not dlc:
        await websocket.send_json({"error": "DLC folder not configured"})
        await websocket.close()
        return

    psarc_path = _resolve_dlc_path(dlc, filename)
    if psarc_path is None:
        await websocket.send_json({"error": "forbidden"})
        await websocket.close()
        return
    if not psarc_path.exists():
        await websocket.send_json({"error": "File not found"})
        await websocket.close()
        return

    is_slop = sloppak_mod.is_sloppak(psarc_path)
    # Sloppak wins precedence: `_extract_meta_for_file()` and the
    # background scanner both treat a `.sloppak` directory as sloppak
    # even if it happens to contain WEM/XML. Gate is_loose on that
    # so the loose-only branches (audio_id, offset, audio conversion)
    # don't fire for sloppak bundles.
    is_loose = (not is_slop) and loosefolder_mod.is_loose_song(psarc_path)
    tmp = None
    owns_tmp = False
    loaded_slop = None  # LoadedSloppak when is_slop
    _keepalive_active = True

    async def _send_keepalives():
        while _keepalive_active:
            try:
                await asyncio.sleep(3)
                if _keepalive_active:
                    await websocket.send_json({"type": "loading", "stage": "Loading..."})
            except Exception:
                break

    try:
        await websocket.send_json({"type": "loading", "stage": "Extracting..."})
        keepalive_task = asyncio.create_task(_send_keepalives())

        try:
            loop = asyncio.get_running_loop()
            _ctx = contextvars.copy_context()
            if is_slop:
                SLOPPAK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                loaded_slop = await loop.run_in_executor(
                    None,
                    lambda: _ctx.run(sloppak_mod.load_song, filename, dlc, SLOPPAK_CACHE_DIR),
                )
                song = loaded_slop.song
                tmp = str(loaded_slop.source_dir)
                owns_tmp = False
            elif is_loose:
                # Loose folders need no extraction — load_song reads the
                # arrangement XMLs directly from the flat directory.
                # psarc_path is already DLC-containment-validated by
                # _resolve_dlc_path, so audio conversion below can use
                # it directly.
                song = await loop.run_in_executor(None, lambda: load_song(str(psarc_path)))
                tmp = str(psarc_path)
                owns_tmp = False
            else:
                tmp, song, owns_tmp = await loop.run_in_executor(
                    None, lambda: _ctx.run(_get_or_extract, filename, psarc_path)
                )
        finally:
            _keepalive_active = False
            keepalive_task.cancel()

        if not song.arrangements:
            await websocket.send_json({"error": "No arrangements found"})
            await websocket.close()
            return

        # Pick arrangement: explicit request > user preference > most notes
        best = -1
        if 0 <= arrangement < len(song.arrangements):
            best = arrangement
        else:
            # Check user's default arrangement preference
            pref = ""
            config_file = CONFIG_DIR / "config.json"
            if config_file.exists():
                try:
                    pref = json.loads(config_file.read_text()).get("default_arrangement", "")
                except Exception:
                    pass
            if pref:
                for i, a in enumerate(song.arrangements):
                    if a.name == pref:
                        best = i
                        break
        if best < 0:
            # Fallback: most notes
            best = 0
            best_count = 0
            for i, a in enumerate(song.arrangements):
                c = len(a.notes) + sum(len(ch.notes) for ch in a.chords)
                if c > best_count:
                    best_count = c
                    best = i
        arr = song.arrangements[best]

        # Convert audio with unique filename (check cache first)
        audio_url = None
        audio_error: str | None = None  # Surfaced in song_info when audio_url is None
        stems_payload: list[dict] = []
        if is_loose:
            # Loose folder filenames are relative paths (artist/album/song).
            # Hash the *canonical* dlc-relative path (so two URL spellings
            # of the same physical folder share a cache key) PLUS the
            # source WEM's mtime+size so:
            #  - different songs with the same leaf folder name can't
            #    collide (a `/`→`__` escape would collapse `a/b__c` and
            #    `a__b/c`);
            #  - editing audio.wem in place invalidates the cached
            #    converted file (without this, in-place CDLC iteration
            #    keeps serving the stale mp3/ogg from the cache).
            try:
                canonical = psarc_path.relative_to(dlc.resolve()).as_posix()
            except ValueError:
                canonical = filename
            wem_for_id = loosefolder_mod.find_audio(psarc_path)
            try:
                wem_stat = wem_for_id.stat() if wem_for_id else None
            except OSError:
                wem_stat = None
            stamp = f"{wem_stat.st_mtime_ns}-{wem_stat.st_size}" if wem_stat else ""
            digest = hashlib.sha256(
                (canonical + "|" + stamp).encode("utf-8")
            ).hexdigest()[:12]
            leaf = Path(canonical.rstrip("/\\")).stem.replace(" ", "_")[:40] or "song"
            audio_id = f"{leaf}_{digest}"
        else:
            audio_id = Path(filename).stem.replace(" ", "_")

        if is_slop:
            # Stems are served via the sloppak file endpoint; the first stem
            # (or explicit default) is the core <audio> source. The stems
            # plugin replaces it with a mixed graph when active.
            from urllib.parse import quote
            q_fn = quote(filename, safe="")
            for s in loaded_slop.stems:
                url = f"/api/sloppak/{q_fn}/file/{quote(s['file'])}"
                stems_payload.append({"id": s["id"], "url": url, "default": s["default"]})
            if stems_payload:
                audio_url = stems_payload[0]["url"]
            else:
                audio_error = "This sloppak has no playable stems."
        else:
            AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            # Check if audio already cached (writable cache dir or legacy static dir)
            for ext in [".mp3", ".ogg", ".wav"]:
                for cache_dir in [AUDIO_CACHE_DIR, STATIC_DIR]:
                    cached_audio = cache_dir / f"audio_{audio_id}{ext}"
                    if cached_audio.exists() and cached_audio.stat().st_size > 1000:
                        audio_url = f"/audio/audio_{audio_id}{ext}"
                        break
                if audio_url:
                    break

        def _evict_audio_cache():
            # Keep AUDIO_CACHE_DIR bounded so a library full of loose
            # folders / many PSARCs doesn't fill disk. LRU on st_atime
            # so songs the user keeps replaying stay warm. Best-effort:
            # log at debug so permission / disk errors are diagnosable
            # without aborting the request.
            try:
                audio_files = [f for f in AUDIO_CACHE_DIR.iterdir()
                               if f.name.startswith("audio_") and f.suffix in (".mp3", ".ogg", ".wav")]
                if len(audio_files) > 100:
                    audio_files.sort(key=lambda f: f.stat().st_atime)
                    for f in audio_files[:len(audio_files) - 100]:
                        f.unlink(missing_ok=True)
            except Exception:
                log.debug("audio cache eviction failed for %s", AUDIO_CACHE_DIR, exc_info=True)

        if not audio_url and is_loose:
            await websocket.send_json({"type": "loading", "stage": "Converting audio..."})
            wem_path = loosefolder_mod.find_audio(psarc_path)
            if wem_path:
                # Re-resolve to defeat a symlinked audio.wem that points
                # outside the song folder — without this, a crafted
                # CDLC could turn convert_wem into an arbitrary-file
                # decode/read primitive.
                wem_resolved = wem_path.resolve()
                try:
                    wem_resolved.relative_to(psarc_path)
                except ValueError:
                    audio_error = "Audio file escapes the loose folder."
                    wem_resolved = None
                if wem_resolved is not None:
                    # Convert into a unique temp basename and then
                    # atomically rename onto the final cache name.
                    # Two clients requesting the same song concurrently
                    # would otherwise race writing the same file and
                    # one could serve a partial mp3/wav.
                    tmp_suffix = uuid.uuid4().hex[:8]
                    tmp_base = AUDIO_CACHE_DIR / f"audio_{audio_id}.{tmp_suffix}"
                    try:
                        produced = convert_wem(str(wem_resolved), str(tmp_base))
                        ext = Path(produced).suffix
                        final_path = AUDIO_CACHE_DIR / f"audio_{audio_id}{ext}"
                        os.replace(produced, final_path)
                        audio_url = f"/audio/audio_{audio_id}{ext}"
                    except Exception as e:
                        log.exception("loose-folder audio conversion failed for %s", audio_id)
                        audio_error = f"Audio conversion failed: {e}"
                        # Best-effort cleanup of partial temp artifacts.
                        for stale in AUDIO_CACHE_DIR.glob(f"audio_{audio_id}.{tmp_suffix}.*"):
                            stale.unlink(missing_ok=True)
            else:
                audio_error = "No audio file found in loose folder."
            _evict_audio_cache()

        if not audio_url and not is_slop and not is_loose:
            await websocket.send_json({"type": "loading", "stage": "Converting audio..."})
            wem_files = find_wem_files(tmp)
            if not wem_files:
                audio_error = "No WEM audio files were found inside this PSARC."
            else:
                try:
                    audio_path = convert_wem(wem_files[0], os.path.join(tmp, "audio"))
                    ext = Path(audio_path).suffix
                    audio_dest = AUDIO_CACHE_DIR / f"audio_{audio_id}{ext}"
                    shutil.copy2(audio_path, audio_dest)
                    audio_url = f"/audio/audio_{audio_id}{ext}"
                except Exception as e:
                    log.exception("audio conversion failed for %s", audio_id)
                    audio_error = f"Audio conversion failed: {e}"

            _evict_audio_cache()

        # Send song metadata
        arr_list = [{"index": i, "name": a.name, "notes": len(a.notes) + sum(len(c.notes) for c in a.chords)}
                    for i, a in enumerate(song.arrangements)]
        await websocket.send_json({
            "type": "song_info",
            "title": song.title,
            "artist": song.artist,
            "duration": song.song_length,
            "arrangement": arr.name,
            "arrangement_index": best,
            "arrangements": arr_list,
            "audio_url": audio_url,
            "audio_error": audio_error,
            "tuning": arr.tuning,
            # Number of strings on the active arrangement
            # (slopsmith-plugin-3dhighway#7). RS XML / PSARC sources
            # always emit `tuning` as length 6 with zero-padding for
            # unused string slots, so `len(arr.tuning)` is unreliable
            # there; sloppak / GP-imported sources may instead carry
            # a trimmed list. arrangement_string_count() combines a
            # notes-derived lower bound, a name-based fallback (4 for
            # "bass" arrangements), and the tuning length (when it
            # disagrees with the RS-XML padded 6) into a single
            # reliable signal. Plugins should size string-indexed UI
            # / geometry against THIS rather than assuming 6 or
            # using `tuning.length` directly.
            "stringCount": arrangement_string_count(arr),
            "capo": arr.capo,
            # Sanitize song.offset before send_json: a malformed loose
            # chart can produce NaN via `float("nan")`, which Starlette
            # would serialise as the literal `NaN` token (invalid JSON)
            # and break the frontend's song_info parsing.
            "offset": _sanitized_song_offset(song) if is_loose else 0.0,
            "format": "sloppak" if is_slop else ("loose" if is_loose else "psarc"),
            "stems": stems_payload,
            # Surface a drum_tab presence flag so the visualization picker
            # can auto-activate the drums plugin even when the chosen
            # arrangement isn't named "Drums" (drum_tab.json lives next
            # to the manifest, not inside the arrangements list).
            "has_drum_tab": bool(
                is_slop and loaded_slop is not None and loaded_slop.drum_tab is not None
            ),
        })

        # Send drum_tab when the sloppak ships one (manifest `drum_tab:` key,
        # see lib/sloppak.py). The drums plugin subscribes to `drum_tab` for
        # the kit legend and `drum_hits` for the timed hit stream. Chunked
        # 500-per-frame like notes so a long song stays well under WS frame
        # limits. Legacy drum sloppaks (drums encoded as guitar notes) skip
        # this branch and fall through to the regular `notes` stream — the
        # client-side drums plugin keeps a fallback decoder for them.
        if is_slop and loaded_slop is not None and loaded_slop.drum_tab is not None:
            dt = loaded_slop.drum_tab
            kit = drums_mod.normalise_kit(dt.get("kit"))
            hits_wire = drums_mod.hits_to_wire(dt.get("hits") or [])
            _dt_name = dt.get("name")
            _dt_name = _dt_name if isinstance(_dt_name, str) and _dt_name else "Drums"
            try:
                await websocket.send_json({
                    "type": "drum_tab",
                    "version": int(dt.get("version", drums_mod.SCHEMA_VERSION)),
                    "name": _dt_name,
                    "kit": kit,
                    "total": len(hits_wire),
                })
                for i in range(0, len(hits_wire), 500):
                    await websocket.send_json({
                        "type": "drum_hits",
                        "data": hits_wire[i:i + 500],
                        "total": len(hits_wire),
                    })
            except WebSocketDisconnect:
                return

        # Send beats
        beats = [{"time": b.time, "measure": b.measure} for b in song.beats]
        await websocket.send_json({"type": "beats", "data": beats})

        # Send sections
        sections = [{"name": s.name, "time": s.start_time} for s in song.sections]
        await websocket.send_json({"type": "sections", "data": sections})

        # Send anchors
        anchors = [anchor_to_wire(a) for a in arr.anchors]
        await websocket.send_json({"type": "anchors", "data": anchors})

        # Send chord templates. Include `fingers` alongside `name` /
        # `frets` so plugin overlays consuming highway.getChordTemplates()
        # can render full chord boxes (Rocksmith-style fingering
        # diagrams), not just chord names. Each fingering entry is
        # per-string: -1 = unused, 0 = open string, n > 0 = finger
        # number. RS XML sources populate real values; GP imports
        # currently emit all -1 (no finger data available pre-import).
        templates = [chord_template_to_wire(ct) for ct in arr.chord_templates]
        await websocket.send_json({"type": "chord_templates", "data": templates})

        # Send lyrics if available
        import xml.etree.ElementTree as ET
        lyrics = []
        # Loose folders are flat — only inspect direct children so a
        # nested backup/export directory inside the song folder can't
        # override the active arrangement's lyrics / tone. PSARCs are
        # unpacked into nested tmp dirs, so they keep recursive rglob.
        # Sloppak skips XML lookups entirely below but the json loop
        # is unconditional, so define both walkers up front.
        _xml_walk = Path(tmp).glob if is_loose else Path(tmp).rglob
        _json_walk = Path(tmp).glob if is_loose else Path(tmp).rglob
        if is_slop:
            lyrics = list(song.lyrics or [])
        else:
            for xml_path in sorted(_xml_walk("*.xml")):
                try:
                    root = ET.parse(xml_path).getroot()
                    if root.tag == "vocals":
                        for v in root.findall("vocal"):
                            lyrics.append({
                                "t": round(float(v.get("time", "0")), 3),
                                "d": round(float(v.get("length", "0")), 3),
                                "w": v.get("lyric", ""),
                            })
                        break
                except Exception:
                    pass
            if not lyrics:
                # SNG-only PSARC (official DLC) — decode vocals SNG directly.
                # Loose folders don't ship SNGs so the loop is a no-op
                # there; same flat-vs-recursive walker choice as XML.
                try:
                    from lib.sng_vocals import parse_vocals_sng
                    for sng_path in sorted(_xml_walk("*vocals*.sng")):
                        plat = "mac" if "/macos/" in str(sng_path).replace("\\", "/").lower() else "pc"
                        try:
                            lyrics = parse_vocals_sng(str(sng_path), plat)
                        except Exception:
                            lyrics = []
                        if lyrics:
                            break
                except ImportError:
                    pass
        if lyrics:
            await websocket.send_json({"type": "lyrics", "data": lyrics})

        # Send tone changes. PSARC and loose folders carry tone data in
        # arrangement XMLs; a sloppak ships it inline in its arrangement JSON
        # (Arrangement.tones, populated by the converter), so read it straight
        # off `arr` rather than walking for XML that doesn't exist.
        if is_slop:
            # `sloppak_tone_changes` builds the (base, sorted changes) pair
            # from `Arrangement.tones`, skipping non-string names and
            # non-finite/non-numeric times — unit-tested in test_tones.py.
            from tones import sloppak_tone_changes
            base_name, tone_changes = sloppak_tone_changes(getattr(arr, "tones", None))
            # Send when there's a base tone OR timed changes — a single-tone
            # arrangement has a base but no switches, and the highway should
            # still be able to show the initial tone.
            if tone_changes or base_name:
                await websocket.send_json({
                    "type": "tone_changes",
                    "base": base_name,
                    "data": tone_changes,
                })
        else:
            xml_paths = sorted(_xml_walk("*.xml"))

            # Build tone ID→name map from the manifest JSON for the selected
            # arrangement. Match on the entry's `ArrangementName` field, not a
            # filename-stem substring — "Lead" is a substring of "Bonus Lead",
            # so the old substring test could build the map from the wrong
            # arrangement. Record the matched JSON stem so the XML below can
            # be paired exactly (RS names the JSON and XML with the same stem).
            arr_tone_names = {}  # the SELECTED arrangement's own Tone_A..D only
            matched_stem = None
            # Strip + lowercase both sides when matching ArrangementName,
            # mirroring lib/tones.py — a manifest with padded whitespace
            # must not fall through to an unrelated arrangement.
            arr_name_lower = arr.name.strip().lower() if arr else ""

            def _manifest_entries(path):
                """Parsed `Entries` dict for a manifest JSON, or {} if the
                file isn't a well-formed manifest (non-dict top level /
                Entries, unparseable JSON)."""
                try:
                    # JSON is UTF-8; decode strictly so malformed bytes fail
                    # cleanly (caught below) rather than silently corrupting
                    # arrangement / tone names.
                    jdata = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    return {}
                entries = jdata.get("Entries") if isinstance(jdata, dict) else None
                return entries if isinstance(entries, dict) else {}

            def _tone_names(attrs):
                """{idx: name} from an entry's Tone_A..Tone_D — string values
                only, so a malformed manifest can't emit a non-string name."""
                m = {}
                for idx, key in enumerate(("Tone_A", "Tone_B", "Tone_C", "Tone_D")):
                    val = attrs.get(key)
                    if isinstance(val, str) and val:
                        m[idx] = val
                return m

            for jf in sorted(_json_walk("*.json")):
                for entry in _manifest_entries(jf).values():
                    if not isinstance(entry, dict):
                        continue
                    attrs = entry.get("Attributes")
                    if not isinstance(attrs, dict):
                        continue
                    ename = attrs.get("ArrangementName")
                    if not isinstance(ename, str) or ename.strip().lower() != arr_name_lower:
                        continue
                    # Only the SELECTED arrangement's own Tone_A..D — never
                    # borrowed from another manifest. An unrelated map would
                    # mislabel `N/A` tone-change markers; `Tone {id}` is the
                    # correct fallback (matching lib/tones.py).
                    arr_tone_names = _tone_names(attrs)
                    matched_stem = jf.stem.lower()
                    break
                if matched_stem is not None:
                    break

            # Parse XMLs. Prefer the XML paired with the matched manifest
            # (identical stem). When no manifest matched (loose/CDLC), fall
            # back to a name-token match — but rank by how few *extra* stem
            # tokens a candidate carries, mirroring lib/tones.py: {"lead"} is
            # a subset of both `song_lead` and `song_bonus_lead`, so a plain
            # subset test still ties. A unique fewest-extra match wins; an
            # exact tie among token candidates is treated as ambiguous —
            # `_token_ambiguous` then suppresses the rank-2 best-effort
            # fallback, so no arrangement's tone timeline is guessed at
            # (matching lib/tones.py, which attaches nothing on a tie).
            # Shared tokenizer with lib/tones.py so PSARC playback and
            # PSARC→sloppak conversion select arrangement XMLs identically.
            from tones import tokens as _name_tokens
            _arr_tokens = _name_tokens(arr.name) if arr else set()
            _token_pick = None
            _token_ambiguous = False
            if _arr_tokens and matched_stem is None:
                _cands = []
                for xp in xml_paths:
                    stem_tokens = _name_tokens(xp.stem)
                    if _arr_tokens <= stem_tokens:
                        _cands.append((len(stem_tokens - _arr_tokens), xp))
                if _cands:
                    _best = min(extra for extra, _ in _cands)
                    _tied = [xp for extra, xp in _cands if extra == _best]
                    if len(_tied) == 1:
                        _token_pick = _tied[0]
                    else:
                        _token_ambiguous = True

            def _xml_rank(xp):
                if matched_stem and xp.stem.lower() == matched_stem:
                    return 0
                if _token_pick is not None and xp == _token_pick:
                    return 1
                return 2
            sorted_xml = sorted(xml_paths, key=lambda xp: (_xml_rank(xp), xp.name))
            # When the arrangement was positively identified (manifest stem
            # pair or a unique token match), tone data must come only from
            # that XML — a rank-2 fallback XML belongs to another
            # arrangement. A token tie is likewise suppressed (guessing among
            # equally-named XMLs would be wrong). Only a genuine no-match
            # case (loose/CDLC with no usable manifest and no name overlap)
            # keeps the long-standing rank-2 best-effort source.
            _suppress_fallback = (
                matched_stem is not None or _token_pick is not None or _token_ambiguous
            )
            sent_tones = False
            psarc_base = ""  # <tonebase> of the preferred arrangement XML
            for xml_path in sorted_xml:
                try:
                    root = ET.parse(xml_path).getroot()
                    if root.tag != "song":
                        continue
                    if _suppress_fallback and _xml_rank(xml_path) == 2:
                        # Don't read tones from an unrelated arrangement's XML.
                        continue
                    # Capture the base tone from the first XML the loop
                    # accepts. The skip above already excluded untrusted
                    # rank-2 XMLs whenever a match was confirmed; in the
                    # genuine no-match case rank-2 IS the best-effort source,
                    # so its <tonebase> is equally valid for a base-only song.
                    if not psarc_base:
                        _tb = root.find("tonebase")
                        if _tb is not None and _tb.text:
                            # Strip whitespace from pretty-printed XML so the
                            # base name matches the sloppak path, which also
                            # strips it.
                            psarc_base = _tb.text.strip()
                    tones_el = root.find("tones")
                    if tones_el is not None:
                        # Accumulate into a per-XML list — if this file
                        # raises partway through, its partial changes are
                        # discarded rather than bleeding into the next
                        # candidate XML.
                        xml_tone_changes = []
                        for t in tones_el.findall("tone"):
                            tc_time = t.get("time")
                            tc_name = t.get("name", "")
                            tc_id = t.get("id", "")
                            # Resolve "N/A" or empty names via the selected
                            # arrangement's own tone map; `Tone {id}` when it
                            # has none (never another arrangement's names).
                            if (not tc_name or tc_name == "N/A") and tc_id:
                                try:
                                    tc_name = arr_tone_names.get(int(tc_id), f"Tone {tc_id}")
                                except (TypeError, ValueError):
                                    pass
                            if tc_time and tc_name:
                                # Skip a single malformed/non-finite marker
                                # rather than letting it raise — the outer
                                # `except` would otherwise swallow the whole
                                # XML and drop every tone change. NaN/inf
                                # would also produce client-unparseable JSON.
                                try:
                                    tc_t = float(tc_time)
                                except (TypeError, ValueError):
                                    continue
                                if not math.isfinite(tc_t):
                                    continue
                                xml_tone_changes.append({
                                    "t": round(tc_t, 3),
                                    "name": tc_name,
                                })
                        if xml_tone_changes:
                            tonebase = root.find("tonebase")
                            base_name = tonebase.text.strip() if tonebase is not None and tonebase.text else ""
                            # If base name not in XML, use the selected
                            # arrangement's own Tone_A.
                            if not base_name:
                                base_name = arr_tone_names.get(0, "")
                            await websocket.send_json({
                                "type": "tone_changes",
                                "base": base_name,
                                "data": sorted(xml_tone_changes, key=lambda x: x["t"]),
                            })
                            sent_tones = True
                            break
                except (ET.ParseError, OSError) as e:
                    # Only swallow unreadable/malformed XML — skip to the next
                    # candidate. A blanket `except` here would also eat a
                    # `WebSocketDisconnect` from `send_json`; let that bubble
                    # to the handler's outer disconnect handler.
                    log.debug(
                        "highway: skipping unreadable arrangement XML %s: %s",
                        xml_path.name, e,
                    )
                    continue
            # Base-only fallback: a single-tone arrangement has a <tonebase>
            # but no <tones> markers — still surface the initial tone so the
            # highway can show it (parity with the sloppak path above).
            # `psarc_base` is the <tonebase> of whichever XML the loop
            # accepted: the confirmed-match XML, or — in the genuine no-match
            # case — the best-effort rank-2 XML. `arr_tone_names` holds the
            # selected arrangement's own Tone_A..D. An ambiguous arrangement
            # (token tie) accepts no XML and has no manifest map, so it
            # correctly sends nothing rather than a guessed tone.
            if not sent_tones:
                base_name = psarc_base
                if not base_name:
                    base_name = arr_tone_names.get(0, "")
                if base_name:
                    await websocket.send_json({
                        "type": "tone_changes",
                        "base": base_name,
                        "data": [],
                    })

        # Send notes in chunks
        notes = [note_to_wire(n) for n in arr.notes]
        # Send in chunks of 500
        for i in range(0, len(notes), 500):
            await websocket.send_json({
                "type": "notes",
                "data": notes[i:i+500],
                "total": len(notes),
            })

        # Send chords
        chords = [chord_to_wire(c) for c in arr.chords]
        for i in range(0, len(chords), 500):
            await websocket.send_json({
                "type": "chords",
                "data": chords[i:i+500],
                "total": len(chords),
            })

        hand_shapes_out = [hand_shape_to_wire(h) for h in arr.hand_shapes]
        for i in range(0, len(hand_shapes_out), 500):
            await websocket.send_json({
                "type": "handshapes",
                "data": hand_shapes_out[i:i+500],
                "total": len(hand_shapes_out),
            })

        # Per-phrase difficulty data for the master-difficulty slider
        # (slopsmith#48). Only sent when the source chart had multiple
        # `<level>` tiers — single-level charts (GP converter, older
        # sloppaks without phrase data) produce arr.phrases=None, and the
        # frontend treats the missing message as "slider disabled".
        # Consumers that don't know about this message type ignore it.
        #
        # Chunked at phrase granularity (20 phrases per frame) because
        # each phrase nests per-level note/chord lists — a single frame
        # could otherwise exceed proxy/WS size limits on large songs.
        # Chunk boundary is per-phrase (not per-level) so the frontend
        # reassembles whole phrase ladders.
        if arr.phrases:
            total = len(arr.phrases)
            for i in range(0, total, 20):
                await websocket.send_json({
                    "type": "phrases",
                    "data": [phrase_to_wire(p) for p in arr.phrases[i:i + 20]],
                    "total": total,
                })

        await websocket.send_json({"type": "ready"})

        # Keep connection alive for control messages
        try:
            while True:
                msg = await websocket.receive_text()
                data = json.loads(msg)
                if data.get("action") == "change_arrangement":
                    pass
        except WebSocketDisconnect:
            pass

    except Exception as e:
        log.exception("highway_ws unhandled error for %s", filename)
        try:
            await websocket.send_json({"error": str(e)})
            await websocket.close()
        except Exception:
            pass

    finally:
        pass  # Don't clean up — cached for arrangement switching


# ── Audio serving ─────────────────────────────────────────────────────────────


@app.get("/api/audio-local-path")
def audio_local_path(url: str, request: Request):
    """Return absolute local filesystem path for an /audio/… URL (Electron desktop only).

    Accepts ``/audio/<path>`` where ``<path>`` may include subdirectory segments —
    no scheme, no host, no query string, no fragment.  The resolved path must stay
    inside AUDIO_CACHE_DIR or STATIC_DIR; ``..`` traversal, backslashes, and
    absolute ``filename`` values are rejected.

    This endpoint returns a raw filesystem path and is intended exclusively for
    the Electron desktop process (which runs on loopback). Requests from non-
    loopback clients are rejected with 403.
    """
    # Loopback-only — only the local Electron process should call this
    client_host = request.client.host if request.client else None
    try:
        is_loopback = bool(client_host and ipaddress.ip_address(client_host).is_loopback)
    except ValueError:
        is_loopback = client_host == "localhost"
    if not is_loopback:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Accept only simple /audio/<filename> — no scheme, no host, no query/fragment
    if not re.fullmatch(r"/audio/[^?#]+", url):
        return JSONResponse({"error": "invalid url"}, status_code=400)
    filename = url[len("/audio/"):]
    # Reject traversal, absolute paths, and backslash separators
    if ".." in filename.split("/") or filename.startswith("/") or "\\" in filename:
        return JSONResponse({"error": "invalid url"}, status_code=400)
    for d in [AUDIO_CACHE_DIR, STATIC_DIR]:
        candidate = (d / filename).resolve()
        # Ensure resolved path is inside the allowed directory
        try:
            candidate.relative_to(d.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return JSONResponse({"path": str(candidate)})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/audio/{filename:path}")
def serve_audio(filename: str):
    """Serve audio files from the writable audio cache directory."""
    # Reject traversal attempts and absolute-path components
    if ".." in filename.split("/") or filename.startswith("/") or "\\" in filename:
        return JSONResponse({"error": "not found"}, status_code=404)
    for d in [AUDIO_CACHE_DIR, STATIC_DIR]:
        candidate = (d / filename).resolve()
        try:
            candidate.relative_to(d.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return FileResponse(str(candidate))
    return JSONResponse({"error": "not found"}, status_code=404)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))
