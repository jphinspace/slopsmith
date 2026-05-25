"""WhisperX-based lyric transcription for vocal stems.

Acts as a fallback path when a PSARC has no vocals XML/SNG, or when the
user runs `transcribe_existing_sloppak()` on a sloppak that lacks
`lyrics.json`. Operates on an already-isolated vocal stem (the Demucs
`vocals.ogg` produced by `split_sloppak_stems`) — does NOT separate
vocals from a mixed track; that's the caller's responsibility.

Output shape matches `_parse_lyrics()` in `sloppak_convert.py` and the
on-disk `lyrics.json` shape documented at `docs/sloppak-spec.md` §2.3:

    [{"t": float, "d": float, "w": str}, ...]

`t` and `d` are seconds. `w` carries a `-` suffix when it joins to the
following syllable; a bare `+` syllable marks a line break. WhisperX
emits words, not syllables; the mapper inserts `+` line breaks on
segment-gap heuristics and otherwise lets each word stand as its own
syllable.

Engine selection
────────────────
Two transcription paths share a common output:

* `transcribe_vocals_remote(path, server_url, ...)` — POST the vocal
  stem to the `/align` endpoint on a slopsmith-demucs-server (Byron's
  reference server already hosts WhisperX alongside Demucs at the same
  URL). Mirrors `_run_demucs_remote()` in `sloppak_convert.py`.

* `transcribe_vocals_local(path, ...)` — load WhisperX in-process. Heavy
  (~3 GB of model weights for `large-v2` + the wav2vec2 aligner) and
  slow on CPU. Deferred imports of `whisperx`, `torch`, and `soundfile`
  keep the rest of slopsmith free of those dependencies — same pattern
  Demucs uses in `sloppak_convert.py:demucs_available()`.

The caller (`_maybe_transcribe_lyrics` in `sloppak_convert.py`) picks
between them based on the converter's `whisperx.server_url` config and
falls back as appropriate. This module does not read config — both
entry points are pure functions of their arguments.

Hallucination mitigation
────────────────────────
Whisper invents plausible-sounding lyrics on near-silent or purely
instrumental input. Two gates guard against that:

1. `vocals_has_signal(path, threshold)` — cheap RMS check before
   inference. Skips songs where the vocal stem is below threshold
   (Demucs returns near-silent vocals for instrumentals).

2. `min_word_score` post-filter — WhisperX's word alignment emits a
   per-word confidence score; words below the threshold are dropped
   from the output. Default 0.35 matches the value the reference
   TabGrabber prototype settled on.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("slopsmith.lib.lyrics_transcribe")

ProgressCB = Optional[Callable[[float, str, str], None]]


# ── Availability probes ──────────────────────────────────────────────────────

def whisperx_available() -> bool:
    """Cheap probe — does this interpreter have whisperx importable?

    Mirror of `demucs_available()` in `sloppak_convert.py`. The local
    transcription path imports whisperx lazily, so this probe lets
    callers gate on availability without paying the full import cost
    (which transitively pulls torch and may try to initialize CUDA)."""
    try:
        import whisperx  # noqa: F401
        return True
    except ImportError:
        return False


# ── Silence gate ─────────────────────────────────────────────────────────────

def vocals_has_signal(vocals_path: Path, threshold: float = 0.005) -> bool:
    """Return True if the vocal stem has RMS energy above `threshold`.

    Cheap pre-check intended to short-circuit transcription on
    instrumentals — Demucs separates instrumental tracks into a
    near-silent vocals stem, and running Whisper on silence produces
    hallucinated lyrics. The default threshold is conservative; a
    truly silent stem reads ~1e-6, normal vocals well above 0.01.

    Returns True when soundfile or numpy is missing (gate is best-effort,
    not a hard requirement). The transcription itself will surface the
    real failure if those deps are needed downstream."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        log.debug("vocals_has_signal: soundfile/numpy missing — skipping gate")
        return True
    try:
        data, _sr = sf.read(str(vocals_path), dtype="float32", always_2d=False)
    except Exception as e:
        log.warning("vocals_has_signal: read of %s failed: %s", vocals_path, e)
        return True
    if data.size == 0:
        return False
    if data.ndim > 1:
        data = data.mean(axis=1)
    rms = float(np.sqrt(np.mean(np.square(data))))
    log.debug("vocals_has_signal: %s rms=%.6f threshold=%.6f", vocals_path.name, rms, threshold)
    return rms >= threshold


# ── Output mapping ───────────────────────────────────────────────────────────

# Gap (in seconds) between WhisperX segments that triggers a `+` line break
# syllable in the sloppak output. Matches the "comfortable visual pause"
# threshold used in TabGrabber. Tighter gaps (between phrases of a single
# verse line) stay on the same line; longer gaps (verse → chorus, etc.)
# get a break so the in-app lyrics overlay can wrap them.
_LINE_BREAK_GAP_SECONDS = 1.5

# Floor on per-word duration in the sloppak output. WhisperX occasionally
# emits zero-length words for very short syllables; the highway overlay's
# fade timing expects a non-zero `d`, so clamp here.
_MIN_WORD_DURATION = 0.05


def _whisperx_to_sloppak(aligned: dict, min_score: float) -> list[dict]:
    """Map WhisperX `aligned` output to sloppak `lyrics.json` shape.

    `aligned` is the dict returned by `whisperx.align()`: a `segments`
    list, each segment carrying a `words` list of `{word, start, end,
    score}` dicts. Drops words below `min_score` (hallucination filter)
    and inserts `+` line-break syllables on segment gaps that exceed
    `_LINE_BREAK_GAP_SECONDS`.

    Times are rounded to 3 decimals to match the convention in
    `sloppak_convert.py:_parse_lyrics()`."""
    out: list[dict] = []
    prev_end: float | None = None
    for segment in aligned.get("segments", []) or []:
        words = segment.get("words") or []
        if not words:
            continue
        # Insert a line break syllable when the gap between this segment
        # and the previous one is comfortably large. Use the first word's
        # start as the break time so it precedes the next syllable in
        # playback order.
        first_start = words[0].get("start")
        if (
            prev_end is not None
            and isinstance(first_start, (int, float))
            and (first_start - prev_end) > _LINE_BREAK_GAP_SECONDS
        ):
            out.append({"t": round(float(first_start), 3), "d": 0.0, "w": "+"})
        last_end_in_seg: float | None = None
        for w in words:
            text = (w.get("word") or "").strip()
            if not text:
                continue
            start = w.get("start")
            end = w.get("end")
            score = w.get("score")
            # Drop words that fail confidence threshold. WhisperX
            # occasionally emits words without a score (e.g. when
            # alignment couldn't localize them); treat those as
            # untrustworthy and drop too.
            if not isinstance(score, (int, float)) or score < min_score:
                continue
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            duration = max(_MIN_WORD_DURATION, float(end) - float(start))
            out.append({
                "t": round(float(start), 3),
                "d": round(duration, 3),
                "w": text,
            })
            last_end_in_seg = float(end)
        if last_end_in_seg is not None:
            prev_end = last_end_in_seg
    return out


# ── Local transcription ─────────────────────────────────────────────────────

def _pick_compute_type(device: str) -> str:
    """Match TabGrabber's compute-type defaults: float16 on CUDA, int8 on CPU.

    WhisperX accepts float16/float32/int8 on CUDA and int8/float32 on CPU.
    int8 is the only viable choice for CPU inference at usable speeds."""
    return "float16" if device == "cuda" else "int8"


def _resolve_device(device: str | None) -> str:
    if device and device != "auto":
        return device
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _free_models(*models) -> None:
    """Release WhisperX models and free GPU memory.

    Called after each transcription to keep memory low between songs in
    batch runs. Safe to call regardless of CUDA availability."""
    for m in models:
        try:
            del m
        except Exception:
            pass
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def transcribe_vocals_local(
    vocals_path: Path,
    *,
    model_size: str = "medium",
    language: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    min_word_score: float = 0.35,
    progress_cb: ProgressCB = None,
) -> list[dict]:
    """Run WhisperX in-process against a vocal stem.

    Deferred whisperx import — callers gate on `whisperx_available()`
    first to avoid the ImportError surfacing here. Heavy: first call
    downloads ~1.5 GB of model weights for `medium` (~3 GB for
    `large-v2`) into the WhisperX cache.

    `model_size` is one of WhisperX's accepted sizes: tiny, base, small,
    medium, large-v2, large-v3. Default `medium` balances accuracy and
    first-run download size; bump to `large-v2` for production quality.

    `language` is an ISO code (e.g. `"en"`); `None` lets WhisperX
    autodetect from the audio.

    `device` is `"cuda"` / `"cpu"` / `None` (auto-detect). `compute_type`
    follows TabGrabber's defaults when `None`."""
    try:
        import whisperx
    except ImportError as e:
        raise RuntimeError(
            "whisperx not installed. Install via the sloppak_converter "
            "plugin's requirements.txt, or `pip install whisperx`."
        ) from e

    resolved_device = _resolve_device(device)
    resolved_compute = compute_type or _pick_compute_type(resolved_device)

    if progress_cb:
        try:
            progress_cb(0.05, "transcribing", f"Loading WhisperX ({model_size}, {resolved_device})")
        except Exception:
            pass

    asr_model = whisperx.load_model(model_size, resolved_device, compute_type=resolved_compute)
    audio = whisperx.load_audio(str(vocals_path))

    if progress_cb:
        try:
            progress_cb(0.30, "transcribing", "Transcribing vocals")
        except Exception:
            pass

    result = asr_model.transcribe(audio, language=language)
    detected_lang = result.get("language") or language or "en"

    if progress_cb:
        try:
            progress_cb(0.60, "transcribing", f"Aligning words ({detected_lang})")
        except Exception:
            pass

    align_model = align_metadata = None
    try:
        align_model, align_metadata = whisperx.load_align_model(
            language_code=detected_lang, device=resolved_device
        )
        aligned = whisperx.align(
            result["segments"], align_model, align_metadata, audio,
            resolved_device, return_char_alignments=False,
        )
    finally:
        _free_models(asr_model, align_model, align_metadata)

    if progress_cb:
        try:
            progress_cb(0.90, "transcribing", "Building lyric tokens")
        except Exception:
            pass

    return _whisperx_to_sloppak(aligned, min_word_score)


# ── Remote transcription ────────────────────────────────────────────────────

def transcribe_vocals_remote(
    vocals_path: Path,
    server_url: str,
    *,
    language: str | None = None,
    api_key: str | None = None,
    timeout: int = 300,
    progress_cb: ProgressCB = None,
) -> list[dict]:
    """POST the vocal stem to `{server_url}/align` and parse the response.

    Mirrors `_run_demucs_remote()` in `sloppak_convert.py`. Expects the
    server to respond with a JSON object carrying a `words` (or
    `segments`) field in WhisperX's native shape; `_whisperx_to_sloppak`
    consumes that directly.

    Errors raise `RuntimeError` with a truncated server response, same
    idiom Demucs uses, so the caller can log+continue without bringing
    down the surrounding split job."""
    import requests

    server_url = server_url.rstrip("/")
    if progress_cb:
        try:
            progress_cb(0.10, "transcribing", f"Uploading to WhisperX server ({server_url})")
        except Exception:
            pass

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    params: dict[str, str] = {}
    if language:
        params["language"] = language

    with open(vocals_path, "rb") as f:
        resp = requests.post(
            f"{server_url}/align",
            files={"file": (vocals_path.name, f, "audio/ogg")},
            params=params,
            headers=headers or None,
            timeout=timeout,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"WhisperX server error ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()

    # Two response shapes are accepted, in this order of preference:
    #
    #   1. Native WhisperX `{"segments": [...]}` — let the standard
    #      mapper handle it (line breaks + score filter + clamps).
    #   2. Pre-flattened sloppak shape `{"words": [{"t","d","w"}, ...]}`
    #      — pass through with rounding for parity with local path.
    #
    # Anything else is an error: surface enough of the response that
    # `_maybe_transcribe_lyrics` can log it and move on.
    if "segments" in data:
        return _whisperx_to_sloppak(data, min_score=0.0)
    if "words" in data:
        return [
            {
                "t": round(float(w["t"]), 3),
                "d": round(float(w["d"]), 3),
                "w": str(w["w"]),
            }
            for w in data["words"]
            if "t" in w and "d" in w and "w" in w
        ]
    raise RuntimeError(f"WhisperX server returned unrecognized shape: {str(data)[:300]}")
