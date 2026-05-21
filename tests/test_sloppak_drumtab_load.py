"""End-to-end test for the sloppak loader recognising a `drum_tab:` manifest
key and surfacing the parsed payload on the LoadedSloppak."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import sloppak as sloppak_mod


def _write_dir_sloppak(root: Path, manifest_extras: dict, drum_tab_payload: dict | None) -> Path:
    """Build a minimal directory-form sloppak that load_song will accept.

    Uses the tmp_path leaf name to make the sloppak filename unique per test,
    avoiding the module-level ``resolve_source_dir`` cache being poisoned by
    a previous test that happened to share the same "song.sloppak" filename.
    """
    pak = root / f"{root.name}.sloppak"
    pak.mkdir()
    arr_dir = pak / "arrangements"
    arr_dir.mkdir()

    # One trivial guitar arrangement — load_song bails on empty arrangements
    # for non-drum-only sloppaks, and the wire format demands at least the
    # standard keys exist (an empty list is fine for each).
    arr = {
        "name": "Lead",
        "tuning": [0, 0, 0, 0, 0, 0],
        "capo": 0,
        "notes": [],
        "chords": [],
        "anchors": [],
        "handshapes": [],
        "templates": [],
        "beats": [],
        "sections": [],
    }
    (arr_dir / "lead.json").write_text(json.dumps(arr))

    manifest = {
        "title": "Test",
        "artist": "Tester",
        "album": "",
        "year": 2026,
        "duration": 10.0,
        "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}],
        "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}],
    }
    manifest.update(manifest_extras)
    (pak / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    if drum_tab_payload is not None:
        (pak / "drum_tab.json").write_text(json.dumps(drum_tab_payload))

    return pak


def _load(pak_path: Path, tmp_path: Path):
    """Invoke sloppak_mod.load_song using its DLC-relative API."""
    dlc_root = pak_path.parent
    cache = tmp_path / "cache"
    cache.mkdir()
    return sloppak_mod.load_song(pak_path.name, dlc_root, cache)


# ── Happy path ───────────────────────────────────────────────────────────────

def test_load_song_attaches_drum_tab_when_manifest_opts_in(tmp_path: Path):
    payload = {
        "version": 1,
        "name": "Drums",
        "kit": [{"id": "kick", "name": "Kick"}, {"id": "snare", "name": "Snare"}],
        "hits": [
            {"t": 0.5, "p": "kick", "v": 110},
            {"t": 0.75, "p": "snare", "v": 92},
        ],
    }
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "drum_tab.json"}, payload)
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is not None
    assert len(loaded.drum_tab["hits"]) == 2
    assert loaded.drum_tab["kit"][0]["id"] == "kick"


def test_load_song_drum_tab_absent_when_manifest_silent(tmp_path: Path):
    pak = _write_dir_sloppak(tmp_path, {}, None)
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is None


def test_load_song_drum_tab_absent_when_file_missing(tmp_path: Path):
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "nope.json"}, None)
    loaded = _load(pak, tmp_path)
    # Pointing at a missing file disables drums silently — don't crash the load.
    assert loaded.drum_tab is None


def test_load_song_drum_tab_absent_when_payload_invalid_json(tmp_path: Path):
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "drum_tab.json"}, None)
    (pak / "drum_tab.json").write_text("not json {{{")
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is None


def test_load_song_drum_tab_absent_when_payload_fails_validation(tmp_path: Path):
    # `hits` must be a list per validate_drum_tab — this is a hard rejection.
    pak = _write_dir_sloppak(
        tmp_path,
        {"drum_tab": "drum_tab.json"},
        {"version": 1, "hits": "not a list"},  # type: ignore[arg-type]
    )
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is None


def test_load_song_drum_tab_round_trips_unknown_pieces(tmp_path: Path):
    """A future piece-id must round-trip — older clients then render it as a
    generic rectangle rather than dropping it."""
    payload = {
        "version": 1,
        "kit": [{"id": "cowbell", "name": "Cowbell"}],
        "hits": [{"t": 1.0, "p": "cowbell", "v": 100}],
    }
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "drum_tab.json"}, payload)
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is not None
    assert loaded.drum_tab["hits"][0]["p"] == "cowbell"


# ── Security / path-traversal branches ──────────────────────────────────────

def test_load_song_drum_tab_absent_when_path_escapes_sloppak(tmp_path: Path):
    """A manifest drum_tab value that escapes the sloppak directory via '../'
    must be silently rejected — drums disabled, load continues."""
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "../outside.json"}, None)
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is None


def test_load_song_drum_tab_absent_when_path_is_absolute(tmp_path: Path):
    """An absolute drum_tab path in the manifest must be silently rejected."""
    pak = _write_dir_sloppak(tmp_path, {"drum_tab": "/etc/passwd"}, None)
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab is None
