"""PSARC → sloppak conversion + stem splitting.

This module is the single source of truth for the convert + split pipelines.
Both the CLI scripts (`scripts/psarc_to_sloppak.py`, `scripts/split_stems.py`)
and the in-app converter plugin (`plugins/sloppak_converter`) import from
here — see the plugin's `routes.py` for the job queue that wraps these
functions with progress reporting.

Each function accepts a `progress_cb(fraction: float, stage: str, message: str)`
callback that the job queue forwards to the client over a WebSocket.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("slopsmith.lib.sloppak_convert")

import yaml

from patcher import unpack_psarc
from sloppak import _unpack_zip as _unpack_sloppak_zip
from song import load_song, arrangement_to_wire
from tones import extract_tones_for_song
from audio import find_wem_files, _vgmstream_cmd, _ffmpeg_cmd, _ffmpeg_wav_to_ogg


ProgressCB = Optional[Callable[[float, str, str], None]]


# Prefix shared by every staging directory this module creates:
# `s2p_extract_`, `s2p_work_`, `s2p_wem_`, `s2p_split_`, `s2p_split_zip_`.
# `cleanup_stale_temp_dirs()` keys off this so a single sweep covers
# everything we leak when the host process is killed mid-conversion.
_TEMP_DIR_PREFIX = "s2p_"


# ── Shared helpers ────────────────────────────────────────────────────────────

def sanitize_stem(name: str) -> str:
    """Filesystem-safe version of a filename stem."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return s or "song"


def _progress(cb: ProgressCB, frac: float, stage: str, msg: str) -> None:
    if cb:
        try:
            cb(frac, stage, msg)
        except Exception as e:
            log.debug("progress callback raised: %s", e)


def _arrangement_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "arr"
    candidate = base
    i = 2
    while candidate in used:
        candidate = f"{base}{i}"
        i += 1
    used.add(candidate)
    return candidate


def _wem_to_ogg(wem_path: str, out_ogg: Path) -> None:
    vgmstream = _vgmstream_cmd()
    ffmpeg = _ffmpeg_cmd()
    if not vgmstream:
        raise RuntimeError("vgmstream-cli not found on PATH")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    with tempfile.TemporaryDirectory(prefix="s2p_wem_") as td:
        wav = Path(td) / "full.wav"
        r = subprocess.run([vgmstream, "-o", str(wav), wem_path], capture_output=True)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 100:
            raise RuntimeError(
                f"vgmstream-cli failed: {r.stderr.decode(errors='replace')}"
            )
        out_ogg.parent.mkdir(parents=True, exist_ok=True)
        r2 = _ffmpeg_wav_to_ogg(ffmpeg, wav, out_ogg)
        if r2.returncode != 0 or not out_ogg.exists() or out_ogg.stat().st_size < 100:
            raise RuntimeError(
                f"ffmpeg OGG encode failed: {r2.stderr.decode(errors='replace')}"
            )


def _parse_lyrics_with_source(extracted_dir: Path) -> tuple[list[dict], str | None]:
    """Return (lyrics, source) where source is "xml" | "sng" | None.

    Caller uses `source` to populate the manifest's `lyrics_source`
    field so downstream UI can distinguish Rocksmith-authored lyrics
    from later auto-transcribed ones (see WhisperX fallback path).

    Empty XML vocals files (root tag matches but zero `<vocal>` entries)
    do NOT short-circuit the SNG fallback — official DLC sometimes
    ships a placeholder XML alongside the real SNG vocals data, and
    treating the empty XML as authoritative would hide the lyrics
    that actually exist."""
    # Try vocals XML first (CDLC and some official DLC)
    for xml_path in sorted(extracted_dir.rglob("*.xml")):
        try:
            root = ET.parse(xml_path).getroot()
        except Exception as e:
            log.debug("lyrics XML parse error in %s: %s", xml_path.name, e)
            continue
        if root.tag != "vocals":
            continue
        lyrics = [
            {
                "t": round(float(v.get("time", "0")), 3),
                "d": round(float(v.get("length", "0")), 3),
                "w": v.get("lyric", ""),
            }
            for v in root.findall("vocal")
        ]
        if lyrics:
            return (lyrics, "xml")
        # Empty `<vocals>` shell — keep scanning. Don't short-circuit
        # to SNG either: another XML file in the extract might be the
        # real one. The outer loop will hit the SNG fallback only if
        # no XML produces tokens.
    # Fall back to vocals SNG (official DLC ships SNG-only)
    try:
        from sng_vocals import parse_vocals_sng
        for sng_path in sorted(extracted_dir.rglob("*vocals*.sng")):
            plat = "mac" if "/macos/" in str(sng_path).replace("\\", "/").lower() else "pc"
            lyrics = parse_vocals_sng(str(sng_path), plat)
            if lyrics:
                return (lyrics, "sng")
    except ImportError:
        pass
    return ([], None)


def _parse_lyrics(extracted_dir: Path) -> list[dict]:
    lyrics, _ = _parse_lyrics_with_source(extracted_dir)
    return lyrics


def _extract_cover(extracted_dir: Path, out_jpg: Path) -> bool:
    dds_files = sorted(
        extracted_dir.rglob("*.dds"), key=lambda p: p.stat().st_size, reverse=True
    )
    if not dds_files:
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        img = Image.open(dds_files[0]).convert("RGB")
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_jpg), "JPEG", quality=88)
        return True
    except Exception as e:
        log.debug("cover art extraction failed: %s", e)
        return False


def _zip_dir(src_dir: Path, out_zip: Path) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(out_zip), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in src_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir).as_posix())


def _newest_mtime_within(dir_path: Path) -> float | None:
    """Return the most recent mtime found anywhere inside `dir_path`
    (including the directory itself), or ``None`` if:

    * ``stat()`` failed on the directory entry itself, or
    * the recursive ``rglob`` walk raised an ``OSError`` mid-iteration.

    Callers treat ``None`` as "unknown activity — do not delete".
    Returning a partial mtime when the walk was interrupted is unsafe:
    traversal may have stopped before reaching a recently-written leaf,
    causing an under-estimate that would incorrectly classify an
    in-flight staging dir as stale.

    Directory mtime on its own is not a reliable activity signal —
    it advances when direct children are added/removed/renamed but
    NOT when files deeper in the tree are written. A Demucs job
    writing under `s2p_split_xxx/htdemucs_6s/<track>/` looks idle
    from the top directory's perspective even mid-run. Walking
    `rglob("*")` and taking the max mtime gives us a real
    "any descendant touched recently" signal. Staging dirs in this
    module hold tens to a few hundred files at most, so the walk
    is cheap; individual entries that fail to stat are skipped
    rather than aborting the whole comparison."""
    try:
        newest = dir_path.stat().st_mtime
    except OSError:
        return None
    try:
        for child in dir_path.rglob("*"):
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    except OSError:
        # rglob can raise mid-iteration on a transient I/O error.
        # Returning the partial mtime accumulated so far is unsafe:
        # if traversal stopped before reaching a recently-written leaf
        # we would under-estimate the newest mtime and could delete an
        # in-flight staging dir. Returning None signals the caller to
        # skip this entry entirely (treat unknown activity as active).
        log.debug("_newest_mtime_within: walk of %s failed, treating as active", dir_path)
        return None
    return newest


def cleanup_stale_temp_dirs(min_age_seconds: float = 0.0) -> int:
    """Sweep `tempfile.gettempdir()` of orphaned `s2p_*` staging dirs left
    behind by killed conversions.

    Each convert / split routine in this module wraps its work in a
    `tempfile.mkdtemp` / `tempfile.TemporaryDirectory` whose prefix
    starts with `_TEMP_DIR_PREFIX` (the per-routine prefixes are the
    specific strings `s2p_extract_`, `s2p_work_`, `s2p_wem_`,
    `s2p_split_`, and `s2p_split_zip_` — the shared `s2p_` head is
    what we match against here so a single sweep covers them all).
    Cleanup of those dirs relies on a `finally` / context-manager
    `__exit__`. Those guarantees do NOT hold if the host process is
    SIGKILL'd (Docker shutdown timeout, OOM kill, `docker compose
    restart` while a job is mid-flight), and the dirs accumulate
    forever. A bulk-convert run that's restarted a few times can
    leave many GB of leftover PSARC extractions under `/tmp` on a
    long-lived container.

    Intended to be called once at host startup, BEFORE any new
    conversion runs in *this* process. Note that startup of this
    process does NOT guarantee filesystem exclusivity:

    - A second slopsmith instance can share `tempfile.gettempdir()`
      (containers writing to a host-mounted `/tmp`, two instances
      run on the same workstation, rolling-restart deployments
      where the old and new processes overlap briefly).
    - An external cleanup pass (cron `tmpwatch`, container init
      hooks) can race with this one.

    `min_age_seconds` is the safety knob for that. The "age" signal
    is the most recent mtime found anywhere inside the staging dir
    (recursive walk), NOT the top-level dir's own mtime — directory
    mtime only advances when direct children are created / removed
    / renamed, so a long Demucs run that writes under
    `s2p_split_xxx/htdemucs_6s/track/` would leave the top dir's
    own mtime stale even while the job is actively writing.
    Recursive `rglob` over a staging dir is cheap (~tens to a few
    hundred files for a real conversion) and gives us a reliable
    "any descendant touched within the threshold" gate.

    Pass a value safely larger than the longest stretch of "no
    visible writes anywhere in the tree" a live conversion might
    have. Server startup hands in 900s (15 minutes), which covers
    the remote Demucs polling window (up to 10 minutes with no
    file writes while the server-side job runs) plus margin for
    upload and download time. For local Demucs / PSARC / WEM
    routines that write continuously, the recursive mtime check
    keeps active dirs alive regardless of threshold.
    The default of 0 is appropriate only for callers who can prove
    filesystem exclusivity (e.g. test harnesses with isolated
    `tmp_path` fixtures).

    For truly hostile shared-`/tmp` environments, the safer answer
    is a per-instance temp root (override via `TMPDIR`); this
    helper is a best-effort backstop, not a substitute.

    Returns the number of directories removed."""
    temp_root = Path(tempfile.gettempdir())
    if not temp_root.is_dir():
        return 0

    import time
    now = time.time()
    removed = 0
    # Stream the directory listing rather than materialize the whole
    # set up front — `/tmp` can hold many thousands of entries on a
    # busy host, and we only ever look at one at a time. `iterdir()`
    # returns a lazy generator, so an `OSError` from the underlying
    # `scandir` can be raised either at construction OR mid-iteration
    # (e.g., the temp dir is unmounted while we're walking it). Wrap
    # the whole `for` so either path lands the helper on the same
    # graceful "give up and report what we got" branch — startup must
    # not crash because /tmp had a transient hiccup.
    try:
        for entry in temp_root.iterdir():
            if not entry.name.startswith(_TEMP_DIR_PREFIX):
                continue
            # Skip files / symlinks — we only ever create directories with
            # this prefix, so anything else under that name is foreign.
            if entry.is_symlink() or not entry.is_dir():
                continue
            if min_age_seconds > 0:
                newest = _newest_mtime_within(entry)
                if newest is None:
                    # Stat failed entirely — safer to skip than to delete
                    # a directory we can't read; the next sweep can retry.
                    continue
                if (now - newest) < min_age_seconds:
                    continue
            try:
                shutil.rmtree(entry, ignore_errors=False)
                removed += 1
            except FileNotFoundError:
                # Race: another process / a concurrent startup removed the
                # directory between `iterdir()` and the `rmtree`. Benign —
                # the end state is what we wanted anyway, so DEBUG only.
                log.debug("cleanup_stale_temp_dirs: %s vanished before removal", entry)
            except OSError as e:
                # A locked file on Windows or a permissions hiccup
                # shouldn't crash startup; log at WARNING and move on so
                # operators can see real failures (vs the benign race
                # above) in the log.
                log.warning("cleanup_stale_temp_dirs: could not remove %s: %s", entry, e)
    except OSError as e:
        # `iterdir()` / `scandir` failed either at construction or
        # mid-iteration. Whatever we already removed stays counted;
        # the next startup pass will retry.
        log.debug("cleanup_stale_temp_dirs: listing %s failed: %s", temp_root, e)
    if removed:
        log.info("cleanup_stale_temp_dirs: removed %d orphaned dir(s) under %s",
                 removed, temp_root)
    return removed


def _remove_path(p: Path) -> None:
    """Remove `p` whether it is a file or a directory; no-op if absent.

    Sloppak outputs can be either zip-form (file) or dir-form
    (directory), so staging / backup paths next to them may need to
    survive crossing between the two forms — e.g. a leftover
    `<out>.sloppak.tmp` file from a killed zip-form convert getting
    cleaned up before a fresh `as_dir=True` convert stages its own
    directory at the same path. Using a single helper keeps the call
    sites symmetric and prevents `NotADirectoryError` /
    `IsADirectoryError` from a mismatched cleanup primitive."""
    if p.is_symlink():
        # Symlinks should never appear inside our staging paths, but if
        # one does, drop the link itself rather than follow it.
        p.unlink(missing_ok=True)
        return
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink(missing_ok=True)


# ── PSARC → sloppak ───────────────────────────────────────────────────────────

def convert_psarc_to_sloppak(
    psarc_path: Path,
    out_path: Path,
    as_dir: bool = False,
    progress_cb: ProgressCB = None,
) -> Path:
    """Convert a PSARC to a .sloppak (single-stem). Returns the output path."""
    _progress(progress_cb, 0.02, "extracting", f"Unpacking {psarc_path.name}")
    tmp_extract = Path(tempfile.mkdtemp(prefix="s2p_extract_"))
    work_dir = Path(tempfile.mkdtemp(prefix="s2p_work_"))
    try:
        unpack_psarc(str(psarc_path), str(tmp_extract))

        _progress(progress_cb, 0.15, "extracting", "Parsing song data")
        song = load_song(str(tmp_extract))
        if not song.arrangements:
            raise RuntimeError("no playable arrangements found in PSARC")

        # Lift tone data (gear definitions + in-song tone changes) out of the
        # unpacked PSARC once — PSARCs keep tones in the manifest JSON /
        # arrangement XML, neither of which survives into the sloppak, so
        # without this the converted sloppak loses all tones. Done in one
        # pass (not per arrangement) to avoid re-scanning the extracted tree.
        try:
            tones_by_arr = extract_tones_for_song(
                tmp_extract, [a.name for a in song.arrangements]
            )
        except Exception as e:
            log.warning("tone extraction failed: %s", e, exc_info=True)
            tones_by_arr = {}

        used_ids: set[str] = set()
        arr_manifest: list[dict] = []
        first = True
        for arr in song.arrangements:
            aid = _arrangement_id(arr.name, used_ids)
            # Attach tones to the Arrangement so arrangement_to_wire owns the
            # serialization (single source of truth for the wire schema).
            arr.tones = tones_by_arr.get(arr.name)
            wire = arrangement_to_wire(arr)
            if first:
                wire["beats"] = [
                    {"time": round(b.time, 3), "measure": b.measure} for b in song.beats
                ]
                wire["sections"] = [
                    {"name": s.name, "number": s.number, "time": round(s.start_time, 3)}
                    for s in song.sections
                ]
                first = False
            arr_file = work_dir / "arrangements" / f"{aid}.json"
            arr_file.parent.mkdir(parents=True, exist_ok=True)
            arr_file.write_text(json.dumps(wire, separators=(",", ":")), encoding="utf-8")
            arr_manifest.append({
                "id": aid,
                "name": arr.name,
                "file": f"arrangements/{aid}.json",
                "tuning": list(arr.tuning),
                "capo": arr.capo,
            })

        _progress(progress_cb, 0.35, "extracting", "Converting audio (WEM → OGG)")
        wems = find_wem_files(str(tmp_extract))
        if not wems:
            raise RuntimeError("no WEM audio found in PSARC")
        _wem_to_ogg(wems[0], work_dir / "stems" / "full.ogg")

        stems_manifest = [{"id": "full", "file": "stems/full.ogg", "default": "on"}]

        lyrics, lyrics_source = _parse_lyrics_with_source(tmp_extract)
        lyrics_rel = None
        if lyrics:
            (work_dir / "lyrics.json").write_text(
                json.dumps(lyrics, separators=(",", ":")), encoding="utf-8"
            )
            lyrics_rel = "lyrics.json"

        cover_rel = None
        if _extract_cover(tmp_extract, work_dir / "cover.jpg"):
            cover_rel = "cover.jpg"

        manifest: dict = {
            "title": song.title or psarc_path.stem,
            "artist": song.artist or "",
            "album": song.album or "",
            "year": int(song.year or 0),
            "duration": round(float(song.song_length or 0.0), 3),
        }
        if cover_rel:
            manifest["cover"] = cover_rel
        manifest["stems"] = stems_manifest
        manifest["arrangements"] = arr_manifest
        if lyrics_rel:
            manifest["lyrics"] = lyrics_rel
            if lyrics_source:
                manifest["lyrics_source"] = lyrics_source
        (work_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        _progress(progress_cb, 0.85, "packing", "Writing output")
        # Atomic write: build the output at a sibling `.tmp` path first,
        # then move/rename onto `out_path`. Without this, a kill mid-write
        # leaves a partial / truncated `.sloppak` (or worse, a half-deleted
        # dir-form output) on disk; the host's library scan keys off the
        # filename, so the broken file shows up as a "real" sloppak until
        # the next successful re-conversion overwrites it.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_path.with_name(out_path.name + ".tmp")
        # Pre-clean stale `.tmp` from a previously-killed convert.
        # `_remove_path` handles either form, so a zip-form `.tmp` left
        # behind by an earlier `as_dir=False` crash doesn't block a new
        # `as_dir=True` stage (or vice versa).
        _remove_path(tmp_out)
        if as_dir:
            shutil.copytree(work_dir, tmp_out)
            # Directory rename-onto-existing isn't portable (`os.replace`
            # raises on Windows when dst is a non-empty dir, and POSIX
            # `rename(2)` only swaps empty dirs). Two-step swap via a
            # `.old` sidecar so the failure window is bounded to one
            # rename; on Windows the dst-exists case is still a brief
            # absence rather than a partial dir.
            backup = out_path.with_name(out_path.name + ".old")
            # Backup slot may pre-exist as either file or dir — could
            # be a leftover from a prior killed `as_dir=True` swap, or
            # a stray file a user dropped there. Clear either form.
            _remove_path(backup)
            # `out_path` itself may pre-exist as either form (user
            # reconverting `as_dir=True` over a previous zip-form
            # sloppak, or vice versa). `rename` on a file works the
            # same as on a dir, so no type sniff needed.
            if out_path.exists():
                out_path.rename(backup)
            try:
                tmp_out.rename(out_path)
            except Exception:
                if backup.exists():
                    backup.rename(out_path)
                raise
            # Backup may itself be either form (we just renamed
            # whatever was at out_path into it); use the helper.
            _remove_path(backup)
        else:
            _zip_dir(work_dir, tmp_out)
            # `os.replace` can swap file-onto-file atomically, but
            # cannot replace a non-empty directory with a file (POSIX
            # `rename(2)` returns ENOTDIR/EISDIR; Windows fails the
            # same way). If `out_path` is a dir-form sloppak left from
            # a prior `as_dir=True` convert, clear it first. The brief
            # absence window between rmtree and os.replace mirrors the
            # dir→dir swap path's bounded gap; without this the convert
            # would crash and the user would have to manually delete
            # the dir to recover.
            if out_path.is_dir():
                _remove_path(out_path)
            os.replace(tmp_out, out_path)

        _progress(progress_cb, 1.0, "done", f"Wrote {out_path.name}")
        return out_path
    finally:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        # Clean up staging sidecars left behind if we bailed before the
        # rename. The happy path already moved `.tmp` onto `out_path`
        # and removed `.old`, so these are no-ops there. The `.old`
        # leg matters specifically for kills after `out_path.rename(backup)`
        # but before `tmp_out.rename(out_path)` in the `as_dir=True` path
        # — without this, a stale `.old` dir accumulates next to the
        # (re-created) `out_path` across crashes.
        for sidecar in (
            out_path.with_name(out_path.name + ".tmp"),
            out_path.with_name(out_path.name + ".old"),
        ):
            _remove_path(sidecar)


# ── Stem splitting via Demucs ────────────────────────────────────────────────

_STEM_ORDER = ["guitar", "bass", "drums", "vocals", "piano", "other"]


def demucs_available() -> bool:
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def _load_converter_config() -> dict:
    """Read `${CONFIG_DIR}/config.json` and return the parsed dict.

    Returns `{}` when the file is missing, unreadable, or the JSON root
    is not an object (e.g. a hand-edited list or scalar). Same graceful
    fallback the legacy `_get_demucs_server_url()` relied on, but tightened
    so callers can rely on the return being a dict — every downstream
    accessor does `.get(...)` and would raise on a list/scalar root."""
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    config_file = config_dir / "config.json"
    if not config_file.exists():
        return {}
    try:
        parsed = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug("failed to read converter config: %s", e)
        return {}
    if not isinstance(parsed, dict):
        log.debug("converter config root must be an object, got %s",
                  type(parsed).__name__)
        return {}
    return parsed


def _get_demucs_server_url() -> str | None:
    """Get the configured remote demucs server URL, if any."""
    url = _load_converter_config().get("demucs_server_url", "") or ""
    if not isinstance(url, str):
        return None
    return url.rstrip("/") or None


def _coerce_float(value: object, default: float) -> float:
    """Best-effort float coercion for config knobs. Hand-edited configs
    often type numerics as strings; tolerate that. Anything that can't
    parse falls through to the default rather than raising."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool) -> bool:
    """Strict bool coercion for config flags. A hand-edited config that
    contains `"enabled": "false"` would otherwise be `bool("false") ==
    True`, silently turning on a feature the user thought they disabled.

    Accepts:
      - real booleans (True / False)
      - case-insensitive strings "true"/"yes"/"on"/"1" → True
      - case-insensitive strings "false"/"no"/"off"/"0" → False
      - real numbers (0 → False, non-zero → True)
    Anything else falls back to `default` instead of raising."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0", ""):
            return False
    return default


def _get_whisperx_config() -> dict:
    """Return the WhisperX sub-config from `${CONFIG_DIR}/config.json`.

    Shape (all keys optional, defaults applied here):

        {
          "enabled": bool,            # default False — opt-in
          "model_size": str,          # default "medium"
          "server_url": str | None,   # default None → fall back to demucs_server_url
          "api_key": str | None,
          "language": str | None,     # ISO code; None = autodetect
          "min_word_score": float,    # default 0.35
          "silence_rms_threshold": float,  # default 0.005
        }

    When `server_url` is unset, callers fall through to
    `_get_demucs_server_url()` — Byron's reference demucs-server
    already hosts WhisperX at `/align`, so the same URL serves both
    workloads for the common single-box deployment.

    Every field is type-coerced so a hand-edited config can't crash the
    split. A `whisperx` key that isn't a dict (or a missing one) yields
    full defaults."""
    cfg = _load_converter_config()
    raw = cfg.get("whisperx")
    if not isinstance(raw, dict):
        raw = {}
    server_url = raw.get("server_url")
    if isinstance(server_url, str):
        server_url = server_url.rstrip("/") or None
    else:
        server_url = None
    api_key = raw.get("api_key") if isinstance(raw.get("api_key"), str) else None
    language = raw.get("language") if isinstance(raw.get("language"), str) else None
    return {
        "enabled": _coerce_bool(raw.get("enabled"), False),
        "model_size": str(raw.get("model_size") or "medium"),
        "server_url": server_url,
        "api_key": api_key or None,
        "language": language or None,
        "min_word_score": _coerce_float(raw.get("min_word_score"), 0.35),
        "silence_rms_threshold": _coerce_float(raw.get("silence_rms_threshold"), 0.005),
    }


def _get_pitch_config() -> dict:
    """Return the karaoke pitch-extraction sub-config from converter config.

    Shape (all keys optional, defaults applied here):

        {
          "enabled": bool,         # default False — opt-in
          "server_url": str | None,  # default None → fall back to demucs_server_url
          "api_key": str | None,
        }

    When `server_url` is unset, callers fall through to
    `_get_demucs_server_url()` — same pattern WhisperX uses, since the
    same demucs server hosts the `/pitch` endpoint alongside `/separate`
    and `/align`."""
    cfg = _load_converter_config()
    raw = cfg.get("pitch_extraction")
    if not isinstance(raw, dict):
        raw = {}
    server_url = raw.get("server_url")
    if isinstance(server_url, str):
        server_url = server_url.rstrip("/") or None
    else:
        server_url = None
    api_key = raw.get("api_key") if isinstance(raw.get("api_key"), str) else None
    return {
        "enabled": _coerce_bool(raw.get("enabled"), False),
        "server_url": server_url,
        "api_key": api_key or None,
    }


def _run_demucs_remote(full_ogg: Path, out_dir: Path, model: str) -> Path:
    """Run stem separation via remote demucs server."""
    import json
    import requests

    server_url = _get_demucs_server_url()
    if not server_url:
        raise RuntimeError("No demucs server configured")

    # Upload the audio file — request all stems the model can produce
    stem_list = "drums,bass,vocals,other,guitar,piano"
    with open(full_ogg, "rb") as f:
        resp = requests.post(
            f"{server_url}/separate",
            files={"file": (full_ogg.name, f, "audio/ogg")},
            params={"model": model, "stems": stem_list},
            timeout=600,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Demucs server error: {resp.text[:300]}")

    data = resp.json()
    stems = data.get("stems", {})
    if not stems:
        # Might be a job-based response — poll for completion
        job_id = data.get("job_id")
        if job_id:
            import time
            for _ in range(120):  # Wait up to 10 minutes
                time.sleep(5)
                jr = requests.get(f"{server_url}/jobs/{job_id}", timeout=30)
                jd = jr.json()
                if jd.get("status") == "complete":
                    stems = jd.get("stems", {})
                    break
                elif jd.get("status") == "failed":
                    raise RuntimeError(f"Demucs server job failed: {jd.get('error')}")

    if not stems:
        raise RuntimeError("Demucs server returned no stems")

    # Download each stem
    result_dir = out_dir / "remote_stems"
    result_dir.mkdir(parents=True, exist_ok=True)
    for stem_name, stem_url in stems.items():
        if stem_url.startswith("/"):
            stem_url = f"{server_url}{stem_url}"
        sr = requests.get(stem_url, timeout=120)
        if sr.status_code == 200:
            ext = ".mp3" if ".mp3" in stem_url else ".wav"
            (result_dir / f"{stem_name}{ext}").write_bytes(sr.content)

    return result_dir


def _run_demucs(full_ogg: Path, out_dir: Path, model: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    config_dir = env.get("CONFIG_DIR", "/config")
    cache_root = Path(config_dir) / "torch_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    env.setdefault("TORCH_HOME", str(cache_root))
    env.setdefault("XDG_CACHE_HOME", str(cache_root))
    # Pin demucs to the bundled ffmpeg/ffprobe. demucs.audio probes the
    # child's PATH for both binaries (ffprobe first, for stream metadata)
    # and falls back to torchcodec when missing; the desktop bundle's
    # torchcodec native shims can't load against the vgmstream-patched
    # FFmpeg DLLs we ship, so the fallback path is broken — we have to
    # keep demucs on the ffmpeg/ffprobe path. Resolves to resources/bin/
    # in desktop builds (lib/sloppak_convert.py → resources/slopsmith/lib
    # → resources/bin). Gate on vgmstream-cli's presence so we don't
    # accidentally prepend a system /bin/ (Docker's `/app/lib/...`
    # resolves parents[2] to `/`, and `/bin/ffprobe` exists there too) —
    # vgmstream-cli is bundled on every desktop platform and isn't a
    # typical system binary, so it's a precise signature for the
    # desktop layout.
    _bundled_bin = Path(__file__).resolve().parents[2] / "bin"
    if any((_bundled_bin / name).is_file() for name in ("vgmstream-cli", "vgmstream-cli.exe")):
        # On Windows the env var is conventionally `Path`, not `PATH`;
        # os.environ is case-insensitive but os.environ.copy() returns a
        # plain dict that preserves whatever casing the OS used. If we
        # blindly write to env["PATH"], Windows ends up with both `Path`
        # and `PATH` keys in the spawned subprocess's env block — and
        # which one wins is implementation-defined. Reuse the existing
        # key's casing (or fall through to "PATH" on Linux/macOS).
        _path_key = next(
            (k for k in env if k.upper() == "PATH"),
            "PATH",
        )
        # Avoid producing a trailing separator when the parent PATH is
        # empty/missing — on some platforms a trailing pathsep implicitly
        # injects the current directory into the search path.
        _existing_path = env.get(_path_key, "")
        env[_path_key] = (
            str(_bundled_bin) + os.pathsep + _existing_path
            if _existing_path
            else str(_bundled_bin)
        )
    # Propagate in-process sys.path additions (plugin loader adds
    # /config/pip_packages at runtime, not via PYTHONPATH) so the child
    # python can also find demucs/torch/torchcodec. PYTHONPATH alone
    # is insufficient on Windows embeddable Python, where the ._pth
    # file forces isolated mode and the env var is ignored — so we
    # also inject sys.path explicitly via a -c bootstrap before
    # delegating to demucs.
    pip_target = str(Path(config_dir) / "pip_packages")
    extra_paths = [p for p in sys.path if p]
    if pip_target not in extra_paths:
        extra_paths.insert(0, pip_target)
    merged = os.pathsep.join(
        extra_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PYTHONPATH"] = merged
    # torchaudio>=2.11 routes .save() through save_with_torchcodec, which
    # requires torchcodec. The desktop bundle's torchcodec native shims can't
    # load against the vgmstream-patched FFmpeg DLLs we ship (see PATH-prepend
    # rationale above), so the save call dies with OSError; on installs where
    # torchcodec was dropped from requirements, .save() raises ImportError.
    # Redirect .save() to soundfile before demucs imports so demucs's per-stem
    # WAV writes work regardless of torchcodec's state. soundfile is NOT a
    # transitive demucs dep — the in-app converter plugin (sloppak_converter
    # >= 1.0.4) ships it via its own requirements.txt, and any other consumer
    # of this module must do the same. The override stays in place even on
    # torchaudio versions that wouldn't need it — soundfile's WAV writes are
    # behaviorally equivalent for demucs's float32 outputs.
    bootstrap = (
        "import sys, json, runpy\n"
        "sys.path[:0] = json.loads(sys.argv[1])\n"
        "sys.argv = [sys.argv[0]] + sys.argv[2:]\n"
        "import torchaudio as _ta, soundfile as _sf, numpy as _np\n"
        "def _ta_save(uri, src, sample_rate, *_a, **_kw):\n"
        # Honor channels_first (torchaudio default True). demucs calls with
        # the default; third-party callers may not.
        "    _cf = _kw.pop('channels_first', True)\n"
        "    a = src.detach().cpu().numpy() if hasattr(src, 'detach') else _np.asarray(src)\n"
        "    if a.ndim == 2 and _cf: a = a.T\n"
        # Pick subtype that preserves bit depth: float32 -> FLOAT,
        # float64 -> DOUBLE (avoid silent 32-bit downcast), int32 ->
        # PCM_32, otherwise PCM_16.
        "    if a.dtype == _np.float64:\n"
        "        _st = 'DOUBLE'\n"
        "    elif a.dtype.kind == 'f':\n"
        "        _st = 'FLOAT'\n"
        "    elif a.dtype == _np.int32:\n"
        "        _st = 'PCM_32'\n"
        "    else:\n"
        "        _st = 'PCM_16'\n"
        "    _sf.write(str(uri), a, int(sample_rate), subtype=_st)\n"
        "_ta.save = _ta_save\n"
        "runpy.run_module('demucs.__main__', run_name='__main__', alter_sys=True)\n"
    )
    cmd = [sys.executable, "-c", bootstrap, json.dumps(extra_paths),
           "-n", model, "-o", str(out_dir), str(full_ogg)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        # demucs writes loader errors to stdout, not stderr -- include both
        # so the surfaced RuntimeError actually points at the cause.
        out_tail = (r.stdout or "").strip().splitlines()[-8:]
        err_tail = (r.stderr or "").strip().splitlines()[-8:]
        tail = " | ".join(out_tail + err_tail) or "(no output)"
        raise RuntimeError(
            f"demucs exited with code {r.returncode}: " + tail
        )
    track_stem = full_ogg.stem
    result_dir = out_dir / model / track_stem
    if not result_dir.exists():
        candidates = list((out_dir / model).iterdir()) if (out_dir / model).exists() else []
        if len(candidates) == 1 and candidates[0].is_dir():
            result_dir = candidates[0]
        else:
            raise RuntimeError(f"demucs output dir not found under {out_dir}/{model}")
    return result_dir


def _encode_ogg(wav_path: Path, ogg_path: Path) -> None:
    ffmpeg = _ffmpeg_cmd() or "ffmpeg"
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    r = _ffmpeg_wav_to_ogg(ffmpeg, wav_path, ogg_path)
    if r.returncode != 0 or not ogg_path.exists():
        raise RuntimeError(
            f"ffmpeg OGG encode failed for {wav_path.name}: "
            f"{r.stderr.decode(errors='replace')}"
        )


def _rewrite_stems_manifest(source_dir: Path, new_stems: list[dict]) -> None:
    mf = source_dir / "manifest.yaml"
    if not mf.exists():
        mf = source_dir / "manifest.yml"
    data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    data["stems"] = new_stems
    mf.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _existing_lyrics_path(source_dir: Path) -> Path | None:
    """Return the on-disk path of an existing lyrics file the manifest
    declares, or None if there isn't one.

    The sloppak format allows `manifest.yaml: lyrics: <any-relpath>`
    so the gate that decides "already has lyrics" can't just check
    `source_dir / lyrics.json` — that misses manifests pointing at
    `lyrics/karaoke.json` or similar. Reads the manifest, resolves
    the `lyrics` key against `source_dir` with the same
    relative-to(source_dir) safety check the sloppak loader uses
    (lib/sloppak.py), and returns the path only when the file
    actually exists on disk and has a `.json` suffix.

    Checks are deliberately surface-level: regular file + `.json`
    extension only. The function does NOT parse the JSON or validate
    that it contains the expected list-of-syllables shape — that
    validation lives in the sloppak loader and would double the cost
    of the gate (read + parse) for no win. A `.json` file that turns
    out to be malformed at load time is the loader's problem; for
    the transcribe gate's purposes, the presence of *any* manifest-
    declared JSON is enough to defer to the user instead of silently
    overwriting it. A manifest that points at a directory or a
    non-JSON file is a broken manifest — return None so the
    transcribe fallback can fix it rather than treating the broken
    value as authoritative "lyrics already present".

    Returns None on any failure mode — no manifest, malformed YAML,
    traversal attempt, missing key, missing file, non-file, wrong
    suffix. Caller treats None as "no usable existing lyrics —
    fallback path may run"."""
    mf = source_dir / "manifest.yaml"
    if not mf.exists():
        mf = source_dir / "manifest.yml"
    if not mf.exists():
        return None
    try:
        data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.debug("_existing_lyrics_path: manifest parse failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    rel = data.get("lyrics")
    if not isinstance(rel, str) or not rel:
        return None
    try:
        candidate = (source_dir / rel).resolve()
        candidate.relative_to(source_dir.resolve())
    except (ValueError, OSError):
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() != ".json":
        return None
    return candidate


def _rewrite_lyrics_manifest(
    source_dir: Path,
    lyrics_rel: str,
    source: str,
    *,
    transcription: dict | None = None,
) -> None:
    """Set `lyrics` + `lyrics_source` on the sloppak's manifest in-place.

    Used by the WhisperX fallback path after writing a fresh
    `lyrics.json`. Caller is responsible for having written the file
    at `source_dir / lyrics_rel` already.

    `transcription` is the optional `lyric_transcription` metadata
    block (engine / model / version) per the same shape the
    stem_separation RFC (slopsmith#357) defines for stems. Set it
    when lyrics came from an automated engine (WhisperX); omit for
    Rocksmith-authored XML/SNG / user-edited lyrics. Removes the
    existing key when explicitly cleared so re-running an authored
    path on top of a previously-auto-transcribed sloppak doesn't
    leave stale provenance behind."""
    mf = source_dir / "manifest.yaml"
    if not mf.exists():
        mf = source_dir / "manifest.yml"
    data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    data["lyrics"] = lyrics_rel
    data["lyrics_source"] = source
    if transcription is not None:
        data["lyric_transcription"] = transcription
    else:
        data.pop("lyric_transcription", None)
    mf.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _load_lyrics_for_pitch(lyrics_path: Path) -> list[dict] | None:
    """Load an existing lyrics JSON for pitch extraction's purposes.

    The pitch endpoint only needs each entry's `t` + `d` (and rejects
    non-numeric values server-side with a 4xx); the rest of the lyrics
    shape (word text, formatting hints, etc.) is forwarded unchanged
    but ignored. So this loader's bar is: return a list of dicts that
    each have a numeric `t` + numeric `d`. Bools are excluded
    explicitly because `isinstance(True, int)` is True in Python and
    the endpoint would reject them. Any other shape (missing fields,
    wrong types, top-level non-list, malformed JSON, IO error) returns
    None so the caller skips the pitch hand-off rather than crashing
    the surrounding transcription pass.

    NOT a manifest reader — caller has already resolved the path via
    `_existing_lyrics_path`. NOT a general lyrics validator — the
    sloppak loader owns shape validation for read-time use."""
    try:
        raw = json.loads(lyrics_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("_load_lyrics_for_pitch: %s read/parse failed: %s", lyrics_path, e)
        return None
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        t = entry.get("t")
        d = entry.get("d")
        if isinstance(t, bool) or isinstance(d, bool):
            continue
        if not isinstance(t, (int, float)) or not isinstance(d, (int, float)):
            continue
        # `json.loads` accepts the non-standard `NaN`/`Infinity`/`-Infinity`
        # literals by default and turns them into Python floats — so the
        # isinstance check above passes them through. Filter explicitly so
        # they don't reach extract_pitch_remote() (which would re-serialize
        # them and trigger a strict-server 4xx, or worse).
        if not math.isfinite(float(t)) or not math.isfinite(float(d)):
            continue
        out.append(entry)
    return out or None


def _maybe_transcribe_lyrics(
    source_dir: Path,
    produced_stems: list[dict],
    *,
    enabled: bool,
    force: bool = False,
    progress_cb: ProgressCB = None,
    base_frac: float = 0.0,
    span_frac: float = 0.1,
) -> bool:
    """Run WhisperX over the freshly-split vocals stem when conditions hold.

    Gates, evaluated in order — any failure short-circuits and the
    surrounding split must NOT fail:

      1. `enabled` is True (explicit per-invocation override, or
         `whisperx.enabled` from converter config).
      2. The Demucs split produced a `vocals.ogg` stem.
      3. The sloppak has no manifest-declared lyrics file (resolved
         via `_existing_lyrics_path()`; not a hardcoded
         `lyrics.json` check). Fallback-only semantics — `force=True`
         bypasses this, used by the retroactive CLI to overwrite
         existing lyrics on user request.
      4. The vocals stem has signal above the configured RMS threshold
         (skip instrumentals — Whisper hallucinates on near-silent
         input).

    Returns True when lyrics were written, False otherwise. All
    exceptions are caught and logged at WARNING — the caller treats
    transcription as best-effort."""
    # Split this step's reserved slice between lyric transcription and
    # the optional pitch extraction that runs on its output. Pitch
    # takes the tail; transcription owns the head. Defined at the top
    # of the function so the inner progress callbacks below (which
    # scale WhisperX's 0..1 into our slice) can rescale into the lyric
    # sub-portion rather than the whole slice — otherwise WhisperX
    # progress could overshoot the lyric/pitch boundary and the bar
    # would visibly move backwards once the lyric phase completes.
    _LYRIC_PORTION = 0.7
    _lyric_span = span_frac * _LYRIC_PORTION
    _pitch_span = span_frac - _lyric_span

    # Helper: emit a "skip" progress update at the end of this step's
    # reserved slice so callers' progress printers / UIs don't stall
    # short of base_frac + span_frac when the transcription bails on
    # any of the gates below.
    def _skip(reason: str) -> bool:
        _progress(progress_cb, base_frac + span_frac, "transcribing", reason)
        return False

    if not enabled:
        # WhisperX disabled, but pitch extraction may still be enabled
        # AND find existing lyrics on disk to work with — don't bail
        # before that flow gets a chance. Note: we only reach this
        # branch when the outer caller actually decided to reserve a
        # slice for us (either wx_enabled OR pitch_enabled in
        # _split_in_dir), so flushing to the slice top on exit is
        # correct either way.
        existing = _existing_lyrics_path(source_dir)
        if existing is not None:
            vocals_path = source_dir / "stems" / "vocals.ogg"
            if vocals_path.exists():
                existing_lyrics = _load_lyrics_for_pitch(existing)
                if existing_lyrics:
                    _maybe_extract_pitch(
                        source_dir, existing_lyrics, vocals_path,
                        progress_cb=progress_cb,
                        base_frac=base_frac,
                        span_frac=span_frac,
                    )
                    return False
        # No pitch hand-off happened — flush the slice the caller
        # reserved for us so the progress bar still reaches the
        # promised boundary.
        _progress(progress_cb, base_frac + span_frac, "transcribing",
                  "Lyric/pitch pass skipped (transcription disabled)")
        return False
    if not any(s.get("id") == "vocals" for s in produced_stems):
        log.debug("_maybe_transcribe_lyrics: no vocals stem in produced output")
        return _skip("No vocals stem to transcribe")
    vocals_path = source_dir / "stems" / "vocals.ogg"
    if not vocals_path.exists():
        log.debug("_maybe_transcribe_lyrics: %s missing despite manifest entry", vocals_path)
        return _skip("Vocals stem missing")
    lyrics_path = source_dir / "lyrics.json"
    # "Already present" gate consults the manifest, not just the
    # `lyrics.json` filename — sloppaks can store lyrics at any
    # manifest-declared path (e.g. `lyrics: karaoke/lyrics.json`).
    # Checking only `source_dir / "lyrics.json"` would silently
    # overwrite an existing entry at a different location and leave
    # the manifest pointing at a new file we just wrote.
    existing_lyrics_path = (None if force else _existing_lyrics_path(source_dir))
    if existing_lyrics_path is not None:
        log.info("_maybe_transcribe_lyrics: %s already has lyrics, skipping (use force=True to override)",
                 source_dir.name)
        # Existing lyrics + a vocals stem + a configured server still
        # satisfy the karaoke pitch pre-condition, even though we're
        # not transcribing here. Load the on-disk lyrics and run pitch
        # so sloppaks whose lyrics came from PSARC xml/sng (or were
        # hand-authored) also get pre-generated karaoke pitch — not
        # just the WhisperX-transcribed ones.
        existing_lyrics = _load_lyrics_for_pitch(existing_lyrics_path)
        if existing_lyrics:
            _maybe_extract_pitch(
                source_dir, existing_lyrics, vocals_path,
                progress_cb=progress_cb,
                base_frac=base_frac,
                span_frac=span_frac,
            )
            # _maybe_extract_pitch flushes to base_frac + span_frac on
            # every exit path, so we don't need an additional _skip flush.
            return False
        return _skip("Lyrics already present")

    cfg = _get_whisperx_config()

    try:
        from lyrics_transcribe import (
            vocals_has_signal,
            transcribe_vocals_remote,
            transcribe_vocals_local,
            whisperx_available,
        )
    except ImportError as e:
        log.warning("_maybe_transcribe_lyrics: lyrics_transcribe import failed: %s", e)
        return _skip("Transcription deps missing")

    if not vocals_has_signal(vocals_path, threshold=cfg["silence_rms_threshold"]):
        log.info("_maybe_transcribe_lyrics: %s vocals below silence threshold — skipping (likely instrumental)",
                 source_dir.name)
        return _skip("Instrumental — vocals stem silent")

    # WhisperX server URL precedence: explicit `whisperx.server_url`
    # wins, else fall back to the demucs server (Byron's reference
    # server hosts WhisperX at /align too), else local in-process.
    server_url = cfg["server_url"] or _get_demucs_server_url()

    _progress(progress_cb, base_frac + _lyric_span * 0.10, "transcribing",
              "Transcribing vocals" + (f" (remote: {server_url})" if server_url else " (local)"))

    def _inner_cb(frac: float, stage: str, msg: str) -> None:
        # Re-scale the transcriber's 0..1 progress into the LYRIC
        # sub-portion of our slice — so the lyric phase tops out at
        # base + _lyric_span and the pitch phase has room to advance
        # the bar further without it visibly moving backwards. Clamp
        # the input so a transcriber that overshoots (e.g. emits >1.0
        # on a "post-processing" stage) can't punch through the lyric
        # boundary and force a backward step on the pitch hand-off.
        clamped = max(0.0, min(1.0, frac))
        _progress(progress_cb, base_frac + _lyric_span * (0.10 + 0.80 * clamped), stage, msg)

    try:
        if server_url:
            lyrics = transcribe_vocals_remote(
                vocals_path, server_url,
                language=cfg["language"],
                api_key=cfg["api_key"],
                min_word_score=cfg["min_word_score"],
                progress_cb=_inner_cb,
            )
        else:
            if not whisperx_available():
                log.warning("_maybe_transcribe_lyrics: whisperx not installed and no server configured — "
                            "skipping. Install whisperx or set whisperx.server_url / demucs_server_url.")
                return _skip("WhisperX unavailable")
            lyrics = transcribe_vocals_local(
                vocals_path,
                model_size=cfg["model_size"],
                language=cfg["language"],
                min_word_score=cfg["min_word_score"],
                progress_cb=_inner_cb,
            )
    except Exception as e:
        log.warning("_maybe_transcribe_lyrics: transcription failed for %s: %s",
                    source_dir.name, e, exc_info=True)
        return _skip(f"Transcription failed: {e}")

    if not lyrics:
        log.info("_maybe_transcribe_lyrics: %s produced no lyrics after filtering", source_dir.name)
        return _skip("No lyrics after filtering")

    # Build the lyric_transcription metadata block per the
    # stem_separation RFC pattern (slopsmith#357). Engine + schema
    # version are always known. `model` is the value we *requested* —
    # the local path uses exactly that; the remote path *should* run
    # the same model on the server but we don't currently introspect
    # the server's response to confirm. A `requested` vs `actual`
    # split (or a separate `lyric_transcription.server_model` field)
    # is a follow-up for when the remote WhisperX server reports its
    # configuration. Documenting it as the requested value here keeps
    # the contract honest until then.
    from lyrics_transcribe import (
        LYRIC_TRANSCRIPTION_ENGINE,
        LYRIC_TRANSCRIPTION_SCHEMA_VERSION,
    )
    transcription_meta: dict = {
        "engine": LYRIC_TRANSCRIPTION_ENGINE,
        "model": cfg["model_size"],
        "version": LYRIC_TRANSCRIPTION_SCHEMA_VERSION,
    }

    # Persist lyrics + update manifest under the same best-effort umbrella as
    # the transcription itself. IO errors (perms, disk full, manifest YAML
    # parse failure on a hand-edited file) must NOT bubble up and abort
    # the surrounding stem-split. Clean up a partially-written lyrics.json
    # so the next pass sees a clean state instead of half-written JSON
    # that the loader would treat as corrupt.
    try:
        lyrics_path.write_text(json.dumps(lyrics, separators=(",", ":")), encoding="utf-8")
        _rewrite_lyrics_manifest(
            source_dir, "lyrics.json", "whisperx",
            transcription=transcription_meta,
        )
    except Exception as e:
        log.warning("_maybe_transcribe_lyrics: failed to persist lyrics for %s: %s",
                    source_dir.name, e, exc_info=True)
        if lyrics_path.exists():
            try:
                lyrics_path.unlink()
            except OSError:
                pass
        return _skip(f"Failed to write lyrics: {e}")

    # Lyric phase complete — flush to the top of its sub-portion
    # (computed up-front as _lyric_span) so the bar settles cleanly
    # at the lyric/pitch boundary before _maybe_extract_pitch takes
    # over the tail. _inner_cb already scales WhisperX into this
    # same sub-portion, so no backward motion is possible here.
    _progress(progress_cb, base_frac + _lyric_span, "transcribing",
              f"Wrote {len(lyrics)} lyric tokens")
    log.info("_maybe_transcribe_lyrics: wrote %d tokens to %s", len(lyrics), lyrics_path)

    # Karaoke pitch extraction is the natural next step now that we
    # have BOTH a vocal stem AND syllable-level lyric timings. Best-
    # effort: failures must not undo the lyric write we just persisted.
    _maybe_extract_pitch(
        source_dir, lyrics, vocals_path,
        progress_cb=progress_cb,
        base_frac=base_frac + _lyric_span,
        span_frac=_pitch_span,
    )
    # Flush to the top of our reserved slice regardless of whether
    # pitch ran, so the caller's progress bar always reaches the
    # boundary the wrapper promised.
    _progress(progress_cb, base_frac + span_frac, "transcribing",
              "Lyric + pitch pass complete")
    return True


def _rewrite_pitch_manifest(
    source_dir: Path,
    pitch_rel: str,
    *,
    extraction: dict | None,
) -> None:
    """Set `vocal_pitch` + optional `pitch_extraction` block on the manifest.

    Used by `_maybe_extract_pitch` after writing a fresh
    `vocal_pitch.json`. Caller is responsible for having written the
    file at `source_dir / pitch_rel` already.

    `extraction` is the optional `pitch_extraction` provenance block
    (engine / model / version) per the same shape `stem_separation`
    and `lyric_transcription` use. Pass it when pitch came from an
    automated engine (currently `crepe` via the demucs server); omit
    for hand-edited pitch tracks. Passing `None` removes any existing
    `pitch_extraction` key so a hand-edit doesn't inherit stale
    auto-generated provenance."""
    mf = source_dir / "manifest.yaml"
    if not mf.exists():
        mf = source_dir / "manifest.yml"
    data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    data["vocal_pitch"] = pitch_rel
    if extraction is not None:
        data["pitch_extraction"] = extraction
    else:
        data.pop("pitch_extraction", None)
    mf.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _maybe_extract_pitch(
    source_dir: Path,
    lyrics: list[dict],
    vocals_path: Path,
    *,
    progress_cb: ProgressCB = None,
    base_frac: float = 0.0,
    span_frac: float = 0.0,
) -> bool:
    """Per-syllable pitch extraction via the demucs server's /pitch endpoint.

    Best-effort sibling to `_maybe_transcribe_lyrics`. Called from
    both the WhisperX success path and the existing-lyrics short-
    circuit inside `_maybe_transcribe_lyrics`, so the vocals gate
    below is enforced here rather than assumed from a single caller.
    Fires when:
      1. `pitch_extraction.enabled` is True in converter config.
      2. There is at least one lyric token (the endpoint needs
         timings — either freshly produced by WhisperX or loaded from
         an existing on-disk lyrics file via `_load_lyrics_for_pitch`).
      3. The vocal stem exists on disk (defensive — both production
         call sites have already checked this, but tests + future
         callers may not).
      4. A server URL is configured (either `pitch_extraction.server_url`
         or the shared `demucs_server_url`). Local CREPE is deferred.

    `progress_cb` / `base_frac` / `span_frac` mirror the same slice
    contract as `_maybe_transcribe_lyrics`: emit progress inside the
    range `[base_frac, base_frac + span_frac]`. Defaults to a
    zero-width slice so direct callers (tests, retroactive CLI) can
    invoke without progress bookkeeping.

    On success writes `vocal_pitch.json` (shape matches what
    byrongamatos/slopsmith-plugin-lyrics-karaoke renders:
    `{"version": 1, "notes": [{"t","d","midi"}, ...]}`) and adds
    `vocal_pitch` + `pitch_extraction` keys to the manifest. On any
    failure logs a warning and returns False — must NOT undo the
    lyric write the surrounding transcription path already persisted."""
    def _flush_skip(reason: str) -> bool:
        # Mirror _maybe_transcribe_lyrics._skip — push the progress bar
        # to the top of our reserved slice on early-exit so callers
        # don't pin short of the boundary.
        _progress(progress_cb, base_frac + span_frac, "pitch", reason)
        return False

    cfg = _get_pitch_config()
    if not cfg["enabled"]:
        log.debug("_maybe_extract_pitch: pitch_extraction.enabled=False — skipping")
        return _flush_skip("Pitch extraction disabled")
    if not lyrics:
        log.debug("_maybe_extract_pitch: no lyrics — nothing to time pitch against")
        return _flush_skip("No lyrics to time pitch against")
    if not vocals_path.exists():
        log.debug("_maybe_extract_pitch: vocals stem missing — skipping")
        return _flush_skip("Vocals stem missing")

    # Server URL precedence: explicit pitch_extraction.server_url first,
    # else fall back to the shared demucs server (which hosts /pitch
    # alongside /separate and /align).
    server_url = cfg["server_url"] or _get_demucs_server_url()
    if not server_url:
        log.info("_maybe_extract_pitch: no server configured "
                 "(pitch_extraction.server_url / demucs_server_url) — skipping. "
                 "Local CREPE fallback not yet implemented.")
        return _flush_skip("No pitch server configured")

    try:
        from vocal_pitch import (
            extract_pitch_remote,
            PITCH_EXTRACTION_ENGINE,
            PITCH_EXTRACTION_MODEL,
            PITCH_EXTRACTION_SCHEMA_VERSION,
        )
    except ImportError as e:
        log.warning("_maybe_extract_pitch: vocal_pitch import failed: %s", e)
        return _flush_skip(f"vocal_pitch import failed: {e}")

    # Scale extract_pitch_remote's internal 0..1 progress into our
    # reserved slice so the overall progress bar advances smoothly
    # through upload + inference, rather than waiting until the call
    # returns to jump.
    def _scaled_progress(frac: float, stage: str, msg: str) -> None:
        clamped = max(0.0, min(1.0, frac))
        _progress(progress_cb, base_frac + span_frac * clamped, stage, msg)

    try:
        notes = extract_pitch_remote(
            vocals_path, lyrics, server_url,
            api_key=cfg["api_key"],
            progress_cb=_scaled_progress,
        )
    except Exception as e:
        log.warning("_maybe_extract_pitch: pitch extraction failed for %s: %s",
                    source_dir.name, e, exc_info=True)
        return _flush_skip(f"Pitch extraction failed: {e}")

    if not notes:
        log.info("_maybe_extract_pitch: %s produced no notes", source_dir.name)
        return _flush_skip("No pitch notes produced")

    pitch_path = source_dir / "vocal_pitch.json"
    pitch_payload = {"version": 1, "notes": notes}
    extraction_meta = {
        "engine": PITCH_EXTRACTION_ENGINE,
        "model": PITCH_EXTRACTION_MODEL,
        "version": PITCH_EXTRACTION_SCHEMA_VERSION,
    }
    try:
        # `allow_nan=False` so non-finite floats raise ValueError here
        # rather than silently writing non-standard `NaN`/`Infinity`
        # tokens that strict JSON consumers (e.g. browsers,
        # `json.loads` with `parse_constant`) would reject. Belt-and-
        # braces — `extract_pitch_remote` already filters non-finite
        # values on the way in, but this catches a future call site
        # that bypasses that loader.
        pitch_path.write_text(
            json.dumps(pitch_payload, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        _rewrite_pitch_manifest(
            source_dir, "vocal_pitch.json", extraction=extraction_meta,
        )
    except Exception as e:
        log.warning("_maybe_extract_pitch: failed to persist pitch for %s: %s",
                    source_dir.name, e, exc_info=True)
        if pitch_path.exists():
            try:
                pitch_path.unlink()
            except OSError:
                pass
        return _flush_skip(f"Failed to write pitch: {e}")

    _progress(progress_cb, base_frac + span_frac, "pitch",
              f"Wrote {len(notes)} pitch notes")
    log.info("_maybe_extract_pitch: wrote %d notes to %s", len(notes), pitch_path)
    return True


def _split_in_dir(
    source_dir: Path,
    model: str,
    progress_cb: ProgressCB,
    base_frac: float,
    span_frac: float,
    transcribe_lyrics: bool | None = None,
) -> None:
    full_ogg = source_dir / "stems" / "full.ogg"
    if not full_ogg.exists():
        raise FileNotFoundError(
            f"{full_ogg} not found — run PSARC conversion first or add stems/full.ogg."
        )

    # Try remote demucs server first, fall back to local
    remote_url = _get_demucs_server_url()
    use_remote = remote_url is not None

    # Reserve the tail of the progress budget for the optional WhisperX
    # transcription + pitch-extraction steps. Demucs gets the bulk
    # (0..split_span); the lyric/pitch pass owns the rest
    # (split_span..1.0). The slice is reserved when EITHER WhisperX OR
    # pitch extraction is enabled — pitch alone is enough to need the
    # tail because it runs on existing lyrics even when WhisperX is
    # disabled (the "Lyrics already present" short-circuit inside
    # _maybe_transcribe_lyrics handles the hand-off).
    wx_enabled = bool(transcribe_lyrics if transcribe_lyrics is not None
                      else _get_whisperx_config()["enabled"])
    pitch_enabled = bool(_get_pitch_config()["enabled"])
    needs_post_split = wx_enabled or pitch_enabled
    split_span = span_frac * (0.85 if needs_post_split else 1.0)

    if use_remote:
        _progress(progress_cb, base_frac + split_span * 0.05, "splitting",
                  f"Sending to Demucs server ({remote_url})")
    else:
        _progress(progress_cb, base_frac + split_span * 0.05, "splitting",
                  f"Running Demucs locally ({model})")

    with tempfile.TemporaryDirectory(prefix="s2p_split_") as td:
        if use_remote:
            try:
                result_dir = _run_demucs_remote(full_ogg, Path(td), model)
            except Exception as e:
                log.warning("Demucs remote failed (%s), falling back to local", e)
                if demucs_available():
                    result_dir = _run_demucs(full_ogg, Path(td), model)
                else:
                    raise RuntimeError(f"Remote demucs failed and local demucs not available: {e}")
        else:
            result_dir = _run_demucs(full_ogg, Path(td), model)

        _progress(progress_cb, base_frac + split_span * 0.85, "splitting",
                  "Encoding split stems")
        produced: list[dict] = []
        stems_dir = source_dir / "stems"
        for wav in sorted(result_dir.glob("*.wav")):
            name = wav.stem.lower()
            out_ogg = stems_dir / f"{name}.ogg"
            _encode_ogg(wav, out_ogg)
            produced.append({"id": name, "file": f"stems/{name}.ogg", "default": "on"})

    if not produced:
        raise RuntimeError("demucs produced no output stems")

    def _order_key(s: dict) -> tuple[int, str]:
        try:
            return (_STEM_ORDER.index(s["id"]), s["id"])
        except ValueError:
            return (len(_STEM_ORDER), s["id"])
    produced.sort(key=_order_key)

    # Optional WhisperX transcription + pitch extraction — runs after
    # stems are encoded but before `full.ogg` is removed (the order
    # doesn't strictly matter for the vocals stem, which is
    # independent, but keeping `full.ogg` around through the
    # transcription call gives a fallback input if a future variant
    # ever needs the mixed track). Wrapped internally so failures
    # don't break the split. The `enabled` argument controls only
    # WhisperX; _maybe_transcribe_lyrics still attempts pitch
    # extraction on existing lyrics when wx is off but pitch is on.
    if needs_post_split:
        _maybe_transcribe_lyrics(
            source_dir,
            produced,
            enabled=wx_enabled,
            progress_cb=progress_cb,
            base_frac=base_frac + split_span,
            span_frac=span_frac - split_span,
        )

    full_ogg.unlink(missing_ok=True)
    _rewrite_stems_manifest(source_dir, produced)

    # Final flush so the caller's progress printer / UI bar always reaches
    # base_frac + span_frac for this stage, even when the transcription
    # pass was disabled (in which case the helper consumed the full
    # span_frac for the split itself).
    _progress(progress_cb, base_frac + span_frac, "splitting", "Split complete")


def split_sloppak_stems(
    sloppak_path: Path,
    model: str = "htdemucs_6s",
    progress_cb: ProgressCB = None,
    base_frac: float = 0.0,
    span_frac: float = 1.0,
    transcribe_lyrics: bool | None = None,
) -> None:
    """Split a sloppak's stems/full.ogg into per-instrument stems via Demucs.

    `transcribe_lyrics` controls the optional WhisperX lyric fallback
    that runs after stems are split. `None` (default) defers to the
    `whisperx.enabled` flag in the converter config; `True` / `False`
    is an explicit per-invocation override. Transcription only fires
    when Demucs produced a `vocals.ogg` AND the sloppak has no
    manifest-declared lyrics file on disk (gate resolved via
    `_existing_lyrics_path()`, not a hardcoded `lyrics.json` check)."""
    if sloppak_path.is_dir():
        _split_in_dir(sloppak_path, model, progress_cb, base_frac, span_frac,
                      transcribe_lyrics=transcribe_lyrics)
        return

    # Zip form: unpack, split, re-zip atomically.
    with tempfile.TemporaryDirectory(prefix="s2p_split_zip_") as td:
        work = Path(td) / "sloppak"
        # Delegate to sloppak's hardened unpack so the convert/split path
        # gets the same zip-slip containment as the player upload path
        # (safe_join per member, root-rejection, per-member fallback).
        _unpack_sloppak_zip(sloppak_path, work)

        _split_in_dir(work, model, progress_cb, base_frac, span_frac * 0.9,
                      transcribe_lyrics=transcribe_lyrics)

        _progress(progress_cb, base_frac + span_frac * 0.95, "packing",
                  "Repacking sloppak")
        tmp_out = sloppak_path.with_suffix(sloppak_path.suffix + ".tmp")
        with zipfile.ZipFile(str(tmp_out), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in work.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(work).as_posix())
        tmp_out.replace(sloppak_path)
        # Final flush so the caller's progress bar reaches 100% for the
        # zip-form path too — the dir-form branch already terminates at
        # base_frac+span_frac inside _split_in_dir.
        _progress(progress_cb, base_frac + span_frac, "done",
                  f"Repacked {sloppak_path.name}")


# ── Retroactive lyric generation on existing sloppaks ───────────────────────

def transcribe_existing_sloppak(
    sloppak_path: Path,
    *,
    force: bool = False,
    model: str = "htdemucs_6s",
    progress_cb: ProgressCB = None,
) -> bool:
    """Add WhisperX-transcribed lyrics to an existing sloppak.

    Three input states are handled:

      1. Sloppak already has `stems/vocals.ogg` (previously split) —
         transcribe directly, no Demucs cost. The common case for
         users who ran the stems splitter without `transcribe_lyrics=True`.

      2. Sloppak only has `stems/full.ogg` (never split) — delegate
         to `split_sloppak_stems(transcribe_lyrics=True)`, which
         Demucs-splits and transcribes in one pass. Leaves the other
         stems in place (no `--vocals-only` discard mode in v1; users
         who don't want stems can keep using the convert path with
         no `--auto-lyrics`).

      3. Sloppak already has lyrics — short-circuit unless
         `force=True`. "Already has lyrics" means the manifest's
         `lyrics` key points at a file that exists on disk
         (resolved via `_existing_lyrics_path()`), regardless of
         filename. Mirrors the fallback-only semantics of the split
         path; `force=True` is the escape hatch.

    Returns True when new lyrics were written, False otherwise. Like
    `_maybe_transcribe_lyrics`, exceptions inside the transcription
    are logged + swallowed; only setup-level errors (missing sloppak,
    missing audio) propagate.

    Works on both directory-form and zip-form sloppaks. Zip-form is
    unpacked to a temp dir, edited, re-zipped atomically (mirrors
    `split_sloppak_stems`'s zip handling)."""
    if not sloppak_path.exists():
        raise FileNotFoundError(f"{sloppak_path} does not exist")

    if sloppak_path.is_dir():
        return _transcribe_existing_in_dir(
            sloppak_path, force=force, model=model, progress_cb=progress_cb,
            base_frac=0.0, span_frac=1.0,
        )

    # Zip form: unpack, edit, repack atomically.
    with tempfile.TemporaryDirectory(prefix="s2p_lyrics_zip_") as td:
        work = Path(td) / "sloppak"
        work.mkdir()
        with zipfile.ZipFile(str(sloppak_path), "r") as zf:
            zf.extractall(work)

        wrote = _transcribe_existing_in_dir(
            work, force=force, model=model, progress_cb=progress_cb,
            base_frac=0.0, span_frac=0.9,
        )
        if not wrote:
            return False

        _progress(progress_cb, 0.95, "packing", "Repacking sloppak")
        tmp_out = sloppak_path.with_suffix(sloppak_path.suffix + ".tmp")
        with zipfile.ZipFile(str(tmp_out), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in work.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(work).as_posix())
        tmp_out.replace(sloppak_path)
        _progress(progress_cb, 1.0, "done", f"Wrote {sloppak_path.name}")
        return True


def _transcribe_existing_in_dir(
    source_dir: Path,
    *,
    force: bool,
    model: str,
    progress_cb: ProgressCB,
    base_frac: float,
    span_frac: float,
) -> bool:
    """Per-directory helper for `transcribe_existing_sloppak`."""
    # Existing-lyrics gate consults the manifest, not just a hardcoded
    # `lyrics.json` filename. The sloppak format lets manifests point at
    # any relpath, so a sloppak with `lyrics: karaoke/lyrics.json` was
    # previously seen as "no lyrics" and would have been overwritten
    # with a fresh `lyrics.json` (plus a manifest rewrite to point at
    # the new path, silently orphaning the old file).
    existing_lyrics_path = _existing_lyrics_path(source_dir)
    vocals_path = source_dir / "stems" / "vocals.ogg"
    full_path = source_dir / "stems" / "full.ogg"
    # The transcribe path writes to a canonical `lyrics.json` regardless
    # of where the previous lyrics lived — we don't try to mirror the
    # old path's quirks. That keeps the post-state predictable for
    # downstream readers (and for the zip-form caller's repack).
    new_lyrics_path = source_dir / "lyrics.json"

    if existing_lyrics_path is not None and not force:
        log.info("transcribe_existing_sloppak: %s already has lyrics at %s "
                 "(pass force=True to override)",
                 source_dir.name, existing_lyrics_path.name)
        return False

    if vocals_path.exists():
        # State 1: vocal stem already isolated. Synthesize a `produced`
        # list with just vocals so `_maybe_transcribe_lyrics` recognizes
        # the stem is available. The stems portion of the manifest is
        # left untouched (we're not rewriting it like the split path
        # does) — only the lyrics/lyrics_source keys get updated by
        # `_maybe_transcribe_lyrics` on a successful pass.
        produced = [{"id": "vocals", "file": "stems/vocals.ogg", "default": "on"}]
        return _maybe_transcribe_lyrics(
            source_dir, produced,
            enabled=True, force=force,
            progress_cb=progress_cb,
            base_frac=base_frac, span_frac=span_frac,
        )

    if not full_path.exists():
        raise FileNotFoundError(
            f"{source_dir} has neither stems/vocals.ogg nor stems/full.ogg — "
            "nothing to transcribe."
        )

    # State 2: only a full mix exists. Delegate to the split path so
    # Demucs produces vocals.ogg and the same transcription gate fires
    # at the end. The transcribe gate inside `_split_in_dir` consults
    # the manifest via `_existing_lyrics_path`, so under `force=True`
    # we have to stash the existing file (whatever its manifest-declared
    # name) out of the way before the split runs, and restore from the
    # in-memory backup if transcription doesn't write a fresh one
    # (Demucs success but Whisper failure / silence gate / config
    # missing). Losing the user's only lyrics because we proactively
    # deleted them and the new pass bailed is the worst possible
    # failure mode here.
    previous_lyrics_bytes: bytes | None = None
    previous_lyrics_target: Path | None = None
    if force and existing_lyrics_path is not None:
        previous_lyrics_bytes = existing_lyrics_path.read_bytes()
        previous_lyrics_target = existing_lyrics_path
        existing_lyrics_path.unlink(missing_ok=True)
    # Snapshot the canonical `lyrics.json` BEFORE the split so we can
    # detect whether transcription actually wrote it (vs an unrelated
    # file with the same name that happened to predate this run).
    # Bare existence-after isn't enough: a sloppak whose manifest
    # points at `karaoke/lyrics.json` could also have a stale
    # `lyrics.json` sitting at the root that the manifest didn't
    # reference — finding it after the split would falsely report
    # success and skip the restore of the manifest-declared file we
    # just unlinked. (mtime_ns + size is sufficient here: the split
    # step runs Demucs which takes minutes, so even coarse FS mtime
    # resolution distinguishes pre and post; a real rewrite changes
    # both fields.)
    def _snapshot(p: Path) -> tuple[int, int] | None:
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None
    pre_snapshot = _snapshot(new_lyrics_path)
    try:
        _split_in_dir(source_dir, model, progress_cb, base_frac, span_frac,
                      transcribe_lyrics=True)
    finally:
        # `wrote_new` is true only when the file's pre/post snapshot
        # actually differs — caught a fresh write — OR the file didn't
        # exist before but does now. Either way, transcription
        # contributed something to disk. Same-snapshot means the
        # transcription was skipped/gated/failed; we restore the
        # manifest-declared backup so the user doesn't lose their
        # existing lyrics.
        post_snapshot = _snapshot(new_lyrics_path)
        wrote_new = post_snapshot is not None and post_snapshot != pre_snapshot
        if previous_lyrics_bytes is not None and previous_lyrics_target is not None and not wrote_new:
            previous_lyrics_target.write_bytes(previous_lyrics_bytes)
    return wrote_new
