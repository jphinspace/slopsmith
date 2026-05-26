"""Tests for lib/vocal_pitch.py — karaoke per-syllable pitch via /pitch endpoint.

Live CREPE inference is too heavy to test in CI (server-side feature),
so these tests stub `requests.post` and verify the request shape +
response parsing. End-to-end positive case is a manual verification
step (see PR description).
"""

from __future__ import annotations

import json

import pytest

import vocal_pitch
from vocal_pitch import extract_pitch_remote


class _FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body


def _stub_post(monkeypatch, captured: dict, response: _FakeResponse):
    """Patch requests.post and capture the call args so tests can assert
    on request shape without standing up an actual HTTP server."""
    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["files"] = files
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return response
    import requests
    monkeypatch.setattr(requests, "post", fake_post)


def test_extract_pitch_remote_happy_path(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"fakeogg")
    lyrics = [{"t": 1.0, "d": 0.5, "w": "hi"}, {"t": 2.0, "d": 0.3, "w": "world"}]
    captured: dict = {}
    _stub_post(monkeypatch, captured, _FakeResponse(json_body={
        "notes": [
            {"t": 1.0, "d": 0.5, "midi": 64},
            {"t": 2.0, "d": 0.3, "midi": 67},
        ],
    }))
    got = extract_pitch_remote(vocals, lyrics, "http://server:7865")
    assert got == [
        {"t": 1.0, "d": 0.5, "midi": 64},
        {"t": 2.0, "d": 0.3, "midi": 67},
    ]
    # URL: server_url + /pitch, no double slash even when server_url has trailing /
    assert captured["url"] == "http://server:7865/pitch"
    # Lyrics serialized in the multipart Form body
    assert json.loads(captured["data"]["lyrics"]) == lyrics


def test_extract_pitch_remote_strips_trailing_slash_on_server_url(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    captured: dict = {}
    _stub_post(monkeypatch, captured, _FakeResponse(json_body={"notes": []}))
    extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865/")
    assert captured["url"] == "http://server:7865/pitch"


def test_extract_pitch_remote_includes_api_key_when_set(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    captured: dict = {}
    _stub_post(monkeypatch, captured, _FakeResponse(json_body={"notes": []}))
    extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}],
                         "http://server:7865", api_key="secret")
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_extract_pitch_remote_no_headers_when_no_api_key(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    captured: dict = {}
    _stub_post(monkeypatch, captured, _FakeResponse(json_body={"notes": []}))
    extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")
    assert captured["headers"] is None


def test_extract_pitch_remote_wraps_request_exception_as_runtimeerror(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    import requests
    def _raise(*a, **kw):
        raise requests.ConnectionError("dns blew up")
    monkeypatch.setattr(requests, "post", _raise)
    with pytest.raises(RuntimeError, match="CREPE server request failed.*dns blew up"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_wraps_oserror_as_runtimeerror(tmp_path):
    # Vocals file deliberately missing — open() raises FileNotFoundError
    # (an OSError subclass) which must surface as RuntimeError per the
    # docstring contract, not leak the raw OSError. Mirrors the
    # network-error wrapping above.
    vocals = tmp_path / "does_not_exist.ogg"
    with pytest.raises(RuntimeError, match="Reading vocals stem.*does_not_exist.ogg failed"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_raises_on_non_200(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(status_code=500, text="server boom"))
    with pytest.raises(RuntimeError, match="CREPE server error.*500.*server boom"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_raises_on_non_json(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body=ValueError("not json")))
    with pytest.raises(RuntimeError, match="non-JSON"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_raises_on_missing_notes_key(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body={"something_else": []}))
    with pytest.raises(RuntimeError, match="unexpected shape"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_raises_on_non_list_notes(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body={"notes": "not a list"}))
    with pytest.raises(RuntimeError, match="not a list"):
        extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")


def test_extract_pitch_remote_filters_malformed_entries(tmp_path, monkeypatch):
    # Server returns a mix of valid + malformed entries. Defensive parser
    # skips the bad ones instead of crashing the whole pass.
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body={"notes": [
        {"t": 1.0, "d": 0.5, "midi": 60},        # ok
        "not a dict",                            # drop
        {"t": 2.0, "d": 0.5},                    # missing midi → drop
        {"t": "not numeric", "d": 0.5, "midi": 65},  # bad t → drop
        {"t": 3.0, "d": 0.3, "midi": 67},        # ok
    ]}))
    got = extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")
    assert got == [
        {"t": 1.0, "d": 0.5, "midi": 60},
        {"t": 3.0, "d": 0.3, "midi": 67},
    ]


def test_extract_pitch_remote_filters_non_finite_server_notes(tmp_path, monkeypatch):
    """A misbehaving server (or numerical edge in CREPE) could surface
    NaN/±Inf for t or d. They must not reach the on-disk vocal_pitch.json
    — strict-JSON consumers (e.g. browser JSON.parse) would reject the
    non-standard NaN/Infinity tokens that allow_nan=True would emit."""
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body={"notes": [
        {"t": 1.0, "d": 0.5, "midi": 64},                # kept
        {"t": float("nan"), "d": 0.5, "midi": 60},       # filtered
        {"t": 0.0, "d": float("inf"), "midi": 60},       # filtered
        {"t": float("-inf"), "d": 0.5, "midi": 60},      # filtered
        {"t": 2.0, "d": 0.3, "midi": 67},                # kept
    ]}))
    got = extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://s:7865")
    assert got == [
        {"t": 1.0, "d": 0.5, "midi": 64},
        {"t": 2.0, "d": 0.3, "midi": 67},
    ]


def test_extract_pitch_remote_rounds_to_three_decimals(tmp_path, monkeypatch):
    vocals = tmp_path / "vocals.ogg"
    vocals.write_bytes(b"")
    _stub_post(monkeypatch, {}, _FakeResponse(json_body={"notes": [
        {"t": 1.234567, "d": 0.876543, "midi": 60},
    ]}))
    got = extract_pitch_remote(vocals, [{"t": 0, "d": 0.1, "w": "x"}], "http://server:7865")
    assert got[0]["t"] == 1.235
    assert got[0]["d"] == 0.877


def test_pitch_extraction_constants_are_stable():
    """Pin the constants so a refactor that silently bumps engine name or
    schema version trips this test instead of shipping a wire break to
    cache consumers."""
    assert vocal_pitch.PITCH_EXTRACTION_ENGINE == "crepe"
    assert vocal_pitch.PITCH_EXTRACTION_MODEL == "v1"
    assert vocal_pitch.PITCH_EXTRACTION_SCHEMA_VERSION == "1.0.0"
