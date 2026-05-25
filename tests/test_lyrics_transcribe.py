"""Tests for lib/lyrics_transcribe.py — WhisperX lyric transcription helpers.

The actual WhisperX inference is too heavy and non-deterministic to test
in CI (multi-GB model download, GPU optional, hallucinations on the
edges). These tests cover the parts that are deterministic and don't
need the model:

* `_whisperx_to_sloppak` — pure dict→dict mapping (score filter,
  line-break heuristic, duration clamp, rounding).
* `vocals_has_signal` — RMS gate over a synthesized WAV.
* `whisperx_available` — graceful False when the package isn't installed.

The end-to-end positive case (PSARC without lyrics → sloppak with
auto-transcribed lyrics) is intentionally a manual verification step;
see the plan's verification section.
"""

from __future__ import annotations

import importlib

import pytest

from lyrics_transcribe import (
    _whisperx_to_sloppak,
    vocals_has_signal,
    whisperx_available,
)


# ── Mapper: scores, line breaks, durations, rounding ────────────────────────

def test_mapper_passes_words_above_score_threshold():
    aligned = {
        "segments": [
            {"words": [
                {"word": "hello", "start": 1.0, "end": 1.3, "score": 0.9},
                {"word": "world", "start": 1.4, "end": 1.7, "score": 0.8},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    assert got == [
        {"t": 1.0, "d": 0.3, "w": "hello"},
        {"t": 1.4, "d": 0.3, "w": "world"},
    ]


def test_mapper_drops_words_below_score_threshold():
    aligned = {
        "segments": [
            {"words": [
                {"word": "good", "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "bad",  "start": 1.3, "end": 1.5, "score": 0.10},
                {"word": "ugly", "start": 1.6, "end": 1.8, "score": 0.05},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    assert [w["w"] for w in got] == ["good"]


def test_mapper_drops_words_with_missing_score():
    # WhisperX occasionally emits words it failed to localize without a
    # score field — those must drop, not pass-through as untrusted text.
    aligned = {
        "segments": [
            {"words": [
                {"word": "scored",   "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "unscored", "start": 1.3, "end": 1.5},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["scored"]


def test_mapper_inserts_line_break_on_segment_gap_above_threshold():
    # Two segments separated by > 1.5s — the mapper drops a `+` syllable
    # at the start of the second segment so the lyrics overlay can wrap.
    aligned = {
        "segments": [
            {"words": [
                {"word": "first", "start": 1.0, "end": 1.2, "score": 0.9},
            ]},
            {"words": [
                {"word": "second", "start": 5.0, "end": 5.3, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got == [
        {"t": 1.0, "d": 0.2, "w": "first"},
        {"t": 5.0, "d": 0.0, "w": "+"},
        {"t": 5.0, "d": 0.3, "w": "second"},
    ]


def test_mapper_does_not_insert_line_break_for_close_segments():
    # Gap of 0.5s — well under the 1.5s threshold, no break syllable.
    aligned = {
        "segments": [
            {"words": [{"word": "a", "start": 1.0, "end": 1.2, "score": 0.9}]},
            {"words": [{"word": "b", "start": 1.7, "end": 1.9, "score": 0.9}]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["a", "b"]


def test_mapper_clamps_zero_duration_to_floor():
    # WhisperX sometimes emits start == end for ultra-short syllables.
    # The lyrics overlay's fade timing needs a non-zero `d`, so the
    # mapper clamps to a small floor.
    aligned = {
        "segments": [
            {"words": [
                {"word": "x", "start": 1.0, "end": 1.0, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got[0]["d"] == 0.05


def test_mapper_rounds_to_three_decimals():
    # Pin the rounding precision so an accidental change doesn't silently
    # shift every downstream lyric timestamp — same contract _parse_lyrics
    # and parse_vocals_sng follow.
    aligned = {
        "segments": [
            {"words": [
                {"word": "hi", "start": 1.234567, "end": 1.876543, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got[0]["t"] == 1.235
    assert got[0]["d"] == 0.642


def test_mapper_skips_empty_word_text():
    # Whitespace-only "word" entries are dropped — the highway overlay
    # would render them as blank syllables otherwise.
    aligned = {
        "segments": [
            {"words": [
                {"word": "   ", "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "real", "start": 1.3, "end": 1.5, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["real"]


def test_mapper_handles_empty_input():
    assert _whisperx_to_sloppak({}, min_score=0.0) == []
    assert _whisperx_to_sloppak({"segments": []}, min_score=0.0) == []
    assert _whisperx_to_sloppak({"segments": [{"words": []}]}, min_score=0.0) == []


# ── Silence gate (uses soundfile + numpy) ───────────────────────────────────

def _make_wav(path, samples, sr: int = 22050):
    sf = pytest.importorskip("soundfile")
    sf.write(str(path), samples, sr)


def test_vocals_has_signal_returns_false_for_silent_wav(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    silent = np.zeros(22050, dtype="float32")
    p = tmp_path / "silent.wav"
    _make_wav(p, silent)
    assert vocals_has_signal(p, threshold=0.005) is False


def test_vocals_has_signal_returns_true_for_loud_wav(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    # 440Hz sine at full scale — clearly above any reasonable threshold.
    t = np.linspace(0, 1.0, 22050, endpoint=False, dtype="float32")
    sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    p = tmp_path / "tone.wav"
    _make_wav(p, sine)
    assert vocals_has_signal(p, threshold=0.005) is True


def test_vocals_has_signal_open_fails_returns_true(tmp_path):
    # Gate is best-effort: when reading the file fails we let downstream
    # surface the real error rather than mis-classify the input as silent.
    pytest.importorskip("soundfile")
    p = tmp_path / "does-not-exist.wav"
    assert vocals_has_signal(p, threshold=0.005) is True


# ── Availability probe ─────────────────────────────────────────────────────

def test_whisperx_available_returns_bool():
    # Doesn't matter whether the test machine has whisperx installed —
    # the probe must return a plain bool, never raise.
    result = whisperx_available()
    assert isinstance(result, bool)


def test_whisperx_available_returns_false_when_import_fails(monkeypatch):
    # Force the import to fail so we exercise the False branch even on
    # machines that happen to have whisperx installed in the test venv.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "whisperx":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Force a fresh module import so the deferred `import whisperx` inside
    # whisperx_available() takes the patched path.
    import lyrics_transcribe
    importlib.reload(lyrics_transcribe)
    assert lyrics_transcribe.whisperx_available() is False
    # Reload again unpatched so we don't leave the patched module in
    # sys.modules for downstream tests.
    monkeypatch.undo()
    importlib.reload(lyrics_transcribe)
