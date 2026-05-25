#!/usr/bin/env python3
"""Split a sloppak's full-mix stem into per-instrument stems via Demucs.

Usage:
    python scripts/split_stems.py path/to/song.sloppak [--model htdemucs_6s]
                                                       [--auto-lyrics]

Takes a sloppak whose only stem is `stems/full.ogg`, runs Demucs to split it
into per-instrument stems, replaces `full.ogg` with the results, and rewrites
`manifest.yaml`.

Accepts both forms:
- Directory-form sloppak: edited in place.
- Zip-form sloppak:       unpacked to a temp dir, edited, re-zipped atomically.

Requires `demucs` to be importable in the current interpreter:
    pip install demucs

Default model is `htdemucs_6s` which produces 6 stems:
    vocals, drums, bass, guitar, piano, other
Override with `--model htdemucs` (4 stems: vocals, drums, bass, other).

Pass --auto-lyrics to additionally run WhisperX on the freshly-split
vocals stem and write `lyrics.json` for sloppaks that lack lyrics.
Requires `whisperx` (local) or a configured demucs/whisperx server.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `lib/` importable regardless of CWD.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "lib"))

from sloppak_convert import split_sloppak_stems  # noqa: E402


def _progress_printer(frac: float, stage: str, msg: str) -> None:
    sys.stdout.write(f"\r[{stage:>12}] {int(frac * 100):3d}%  {msg:<60}")
    sys.stdout.flush()
    if frac >= 1.0:
        sys.stdout.write("\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(
        description="Split a sloppak's full-mix into per-instrument stems via Demucs"
    )
    ap.add_argument("sloppak", type=Path, help="input .sloppak (file or directory)")
    ap.add_argument("--model", default="htdemucs_6s",
                    help="demucs model (default: htdemucs_6s = 6 stems inc. guitar + piano; "
                         "htdemucs = 4 stems without guitar)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--auto-lyrics", dest="auto_lyrics", action="store_true",
                     default=None,
                     help="also run WhisperX on the vocals stem and write lyrics.json")
    grp.add_argument("--no-auto-lyrics", dest="auto_lyrics", action="store_false",
                     help="disable lyric transcription even if enabled in config")
    args = ap.parse_args()

    if not args.sloppak.exists():
        print(f"error: {args.sloppak} does not exist", file=sys.stderr)
        return 2

    try:
        split_sloppak_stems(
            args.sloppak,
            model=args.model,
            progress_cb=_progress_printer,
            transcribe_lyrics=args.auto_lyrics,
        )
    except Exception as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
