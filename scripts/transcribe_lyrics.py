#!/usr/bin/env python3
"""Add WhisperX-transcribed lyrics to an existing sloppak.

Usage:
    python scripts/transcribe_lyrics.py path/to/song.sloppak
    python scripts/transcribe_lyrics.py path/to/song.sloppak --force
    python scripts/transcribe_lyrics.py path/to/dir/        # batch over dir of sloppaks

Behaviour by input state:

  1. Sloppak already has stems/vocals.ogg → transcribe directly. Fast path.
  2. Sloppak only has stems/full.ogg     → run Demucs to extract vocals,
                                            then transcribe. Keeps other
                                            split stems as a side effect.
  3. Sloppak already has lyrics          → skip with a message; pass
     (any manifest-declared path,         --force to overwrite (the
     not just lyrics.json)                v1 fallback-only default is
                                          opt-out via this flag). On
                                          overwrite, the fresh transcript
                                          always lands at canonical
                                          stems/lyrics.json and the
                                          manifest gets repointed.

Requires whisperx for the local path, or a configured remote demucs server
(which hosts /align too) via $CONFIG_DIR/config.json:

    {
      "demucs_server_url": "http://...",       // reused for WhisperX /align
      "whisperx": {"enabled": true, "model_size": "medium"}
    }

The transcription step honours the `whisperx` config sub-section the same
way the converter does. `--force` only bypasses the existing-lyrics gate;
it does not enable WhisperX itself — the script unconditionally forces
the transcription on (overriding the config's `enabled: false` default),
since running the script at all is an explicit opt-in.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `lib/` importable regardless of CWD — matches the pattern in
# scripts/split_stems.py and scripts/psarc_to_sloppak.py.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "lib"))

from sloppak_convert import transcribe_existing_sloppak  # noqa: E402


def _progress_printer(frac: float, stage: str, msg: str) -> None:
    sys.stdout.write(f"\r[{stage:>12}] {int(frac * 100):3d}%  {msg:<60}")
    sys.stdout.flush()
    if frac >= 1.0:
        sys.stdout.write("\n")


def _process_one(path: Path, *, force: bool, model: str) -> int:
    print(f"[*] {path.name}")
    try:
        wrote = transcribe_existing_sloppak(
            path, force=force, model=model, progress_cb=_progress_printer,
        )
    except Exception as e:
        print(f"[!] {path.name}: {e}", file=sys.stderr)
        return 1
    if wrote:
        print(f"[ok] {path.name}: lyrics written")
        return 0
    # transcribe_existing_sloppak returns False for any non-success path:
    # existing lyrics + no force, instrumental, missing whisperx, remote
    # server failure, no tokens after filtering. The underlying reason is
    # logged via slopsmith.lib.* (basicConfig wires those to stdout in
    # main()), so keep this surface message generic and point users at
    # the log lines for specifics.
    print(f"[--] {path.name}: no lyrics written — see log above for reason")
    return 0


def _iter_sloppaks(path: Path):
    """Yield sloppak paths under `path`.

    `path` may be a single sloppak (file or directory) or a directory
    containing many sloppaks (batch mode). Directories whose own name
    ends in `.sloppak` are treated as a single sloppak (dir-form);
    every other directory is scanned non-recursively for `*.sloppak`
    children and `.sloppak/` subdirectories."""
    if path.is_file() and path.suffix == ".sloppak":
        yield path
        return
    if path.is_dir() and path.suffix == ".sloppak":
        yield path
        return
    if path.is_dir():
        for child in sorted(path.iterdir()):
            if child.suffix == ".sloppak":
                yield child


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(
        description="Transcribe vocals to lyrics.json for existing sloppaks via WhisperX."
    )
    ap.add_argument("target", type=Path,
                    help="sloppak (file or directory form) or a directory containing many")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing lyrics (any manifest-declared lyrics path) "
                         "and write a fresh canonical lyrics.json. Default: skip songs "
                         "whose manifest already declares a lyrics file.")
    ap.add_argument("--model", default="htdemucs_6s",
                    help="demucs model for state-2 (no vocals stem yet) splits. "
                         "Default htdemucs_6s.")
    args = ap.parse_args()

    if not args.target.exists():
        print(f"error: {args.target} does not exist", file=sys.stderr)
        return 2

    sloppaks = list(_iter_sloppaks(args.target))
    if not sloppaks:
        print(f"error: no .sloppak found under {args.target}", file=sys.stderr)
        return 2

    failures = 0
    for s in sloppaks:
        rc = _process_one(s, force=args.force, model=args.model)
        if rc != 0:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
