"""Tests for lib/sloppak_convert.py pure helpers (sanitize_stem, _arrangement_id)
and the demucs subprocess wiring (_run_demucs PATH-pinning + error capture).
"""

import os
import subprocess
from types import SimpleNamespace

import pytest

import sloppak_convert
from sloppak_convert import sanitize_stem, _arrangement_id


# ── sanitize_stem ────────────────────────────────────────────────────────────
# Regex replaces [^A-Za-z0-9._-]+ with "_", strips leading/trailing "_",
# falls back to "song" for empty result.

SANITIZE_CASES = [
    ("cleanname", "cleanname"),                          # passthrough
    ("song_v2.mp3", "song_v2.mp3"),                      # dot and underscore preserved
    ("safe-name.ogg", "safe-name.ogg"),                  # hyphen preserved
    ("my track", "my_track"),                            # space -> underscore
    ("my   track", "my_track"),                          # run of spaces collapses to one _
    ("path/to/file", "path_to_file"),                    # slashes -> underscore
    ("weird!@#name", "weird_name"),                      # punctuation run collapses to one _
    ("  spaced  ", "spaced"),                            # leading/trailing _ stripped
    ("__both__", "both"),                                # chained _ stripped at ends
    ("___", "song"),                                     # all-underscore input -> fallback
    ("", "song"),                                        # empty input -> fallback
    ("!!!", "song"),                                     # all-forbidden-chars -> fallback
    ("ünicöde", "nic_de"),                               # non-ASCII chars collapse to "_" via [^A-Za-z0-9._-]
]


@pytest.mark.parametrize("raw,expected", SANITIZE_CASES)
def test_sanitize_stem(raw, expected):
    assert sanitize_stem(raw) == expected


# ── _arrangement_id ──────────────────────────────────────────────────────────
# Regex replaces [^a-z0-9]+ with "_" in the lowercased input, strips "_",
# falls back to "arr" for empty. On collision, appends 2/3/… and mutates
# the `used` set in place.

def test_arrangement_id_first_call_passes_through():
    used = set()
    assert _arrangement_id("Lead", used) == "lead"
    assert used == {"lead"}


def test_arrangement_id_deduplicates_on_collision():
    used = {"lead"}
    assert _arrangement_id("Lead", used) == "lead2"
    assert used == {"lead", "lead2"}


def test_arrangement_id_chains_through_multiple_collisions():
    used = {"lead", "lead2"}
    assert _arrangement_id("Lead", used) == "lead3"
    assert used == {"lead", "lead2", "lead3"}


def test_arrangement_id_lowercases_and_strips_punctuation():
    used = set()
    assert _arrangement_id("Part Bass-01", used) == "part_bass_01"


def test_arrangement_id_empty_input_falls_back_to_arr():
    used = set()
    assert _arrangement_id("", used) == "arr"
    assert used == {"arr"}


def test_arrangement_id_all_punctuation_falls_back_to_arr():
    used = set()
    assert _arrangement_id("!!!", used) == "arr"
    assert used == {"arr"}


def test_arrangement_id_mutates_the_used_set_in_place():
    used = set()
    _arrangement_id("Rhythm", used)
    _arrangement_id("Rhythm", used)
    _arrangement_id("Rhythm", used)
    assert used == {"rhythm", "rhythm2", "rhythm3"}


# ── _run_demucs PATH pinning + error capture ────────────────────────────────
# On Windows desktop builds, demucs's audio loader spawns ffprobe before
# ffmpeg. The bundled binaries live in resources/bin/, two parents up from
# this lib/ file. We pin them onto the child's PATH so demucs always finds
# them, and we fold stdout into RuntimeError messages because demucs writes
# its loader errors to stdout.

def _stub_subprocess_run(captured: dict, *, returncode=0, stdout="", stderr=""):
    def fake_run(cmd, env=None, capture_output=False, text=False, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["capture_output"] = capture_output
        captured["text"] = text
        # Guard against regressions: _run_demucs must capture both streams in
        # text mode, otherwise the RuntimeError tails come back as bytes or
        # are dropped entirely (re-introducing the empty-error-output bug
        # this PR fixes).
        assert capture_output, "_run_demucs must call subprocess.run with capture_output=True"
        assert text, "_run_demucs must call subprocess.run with text=True"
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return fake_run


def test_run_demucs_prepends_bundled_bin_to_path(tmp_path, monkeypatch):
    """When the desktop bundle layout is detected, _run_demucs prepends
    resources/bin/ to the child env's PATH. The detection signature is
    a vgmstream-cli marker file inside the candidate bin dir."""
    fake_resources = tmp_path / "resources"
    fake_lib = fake_resources / "slopsmith" / "lib"
    fake_lib.mkdir(parents=True)
    fake_bin = fake_resources / "bin"
    fake_bin.mkdir()
    (fake_bin / "vgmstream-cli").write_text("")  # desktop-bundle marker

    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    monkeypatch.setenv("PATH", "/preexisting/path")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    out_dir = tmp_path / "out"
    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    # _run_demucs raises afterwards because the result dir doesn't exist
    # under the stub — we only care about the env that was passed to run.
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, out_dir, model="htdemucs_6s")

    env = captured["env"]
    assert env is not None
    path_parts = env["PATH"].split(os.pathsep)
    assert path_parts[0] == str(fake_bin)
    assert "/preexisting/path" in path_parts


def test_run_demucs_prepends_bundled_bin_does_not_introduce_empty_pathsep(tmp_path, monkeypatch):
    """When the parent PATH is empty/missing, the prepend must not produce
    a trailing pathsep — that implicitly injects the current directory
    into the search path on some platforms."""
    fake_resources = tmp_path / "resources"
    fake_lib = fake_resources / "slopsmith" / "lib"
    fake_lib.mkdir(parents=True)
    fake_bin = fake_resources / "bin"
    fake_bin.mkdir()
    (fake_bin / "vgmstream-cli").write_text("")

    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    assert captured["env"]["PATH"] == str(fake_bin)
    assert not captured["env"]["PATH"].endswith(os.pathsep)


def test_run_demucs_preserves_windows_path_var_casing(tmp_path, monkeypatch):
    """On Windows os.environ.copy() returns a dict containing `Path`
    (the OS's native casing) rather than `PATH`. Blindly writing
    `env["PATH"] = ...` leaves the original `Path` key untouched and
    spawns a subprocess with both keys in its env block — Windows
    resolution is then implementation-defined. The implementation must
    reuse whatever PATH-equivalent key was already there."""
    fake_resources = tmp_path / "resources"
    fake_lib = fake_resources / "slopsmith" / "lib"
    fake_lib.mkdir(parents=True)
    fake_bin = fake_resources / "bin"
    fake_bin.mkdir()
    (fake_bin / "vgmstream-cli.exe").write_text("")

    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    # Remove every existing PATH-equivalent and seed only `Path` (Windows shape).
    for key in [k for k in os.environ if k.upper() == "PATH"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("Path", "C:\\Windows\\system32;C:\\Windows")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    env = captured["env"]
    # Exactly one PATH-equivalent key must exist, and it must reuse the
    # original `Path` casing rather than introduce a sibling `PATH`.
    path_keys = [k for k in env if k.upper() == "PATH"]
    assert path_keys == ["Path"], f"expected only 'Path', got {path_keys!r}"
    assert env["Path"].split(os.pathsep)[0] == str(fake_bin)
    assert "C:\\Windows\\system32" in env["Path"]


def test_run_demucs_prepends_bundled_bin_with_windows_marker(tmp_path, monkeypatch):
    """vgmstream-cli.exe (Windows desktop bundle) also satisfies the marker check."""
    fake_resources = tmp_path / "resources"
    fake_lib = fake_resources / "slopsmith" / "lib"
    fake_lib.mkdir(parents=True)
    fake_bin = fake_resources / "bin"
    fake_bin.mkdir()
    (fake_bin / "vgmstream-cli.exe").write_text("")

    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    monkeypatch.setenv("PATH", "/preexisting/path")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    assert captured["env"]["PATH"].split(os.pathsep)[0] == str(fake_bin)


def test_run_demucs_no_op_when_bundled_bin_missing(tmp_path, monkeypatch):
    """No resources/bin/ → PATH is left untouched (dev / non-desktop case)."""
    fake_lib = tmp_path / "no-bundle" / "lib"
    fake_lib.mkdir(parents=True)
    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    monkeypatch.setenv("PATH", "/preexisting/path")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    assert captured["env"]["PATH"] == "/preexisting/path"


def test_run_demucs_no_op_when_bin_lacks_desktop_marker(tmp_path, monkeypatch):
    """Docker case: parents[2] resolves to /, /bin exists with system
    binaries, but vgmstream-cli is absent — must not prepend."""
    fake_resources = tmp_path / "resources"
    fake_lib = fake_resources / "slopsmith" / "lib"
    fake_lib.mkdir(parents=True)
    fake_bin = fake_resources / "bin"
    fake_bin.mkdir()
    # Simulate /bin: ffmpeg/ffprobe present, vgmstream-cli absent.
    (fake_bin / "ffmpeg").write_text("")
    (fake_bin / "ffprobe").write_text("")

    monkeypatch.setattr(sloppak_convert, "__file__", str(fake_lib / "sloppak_convert.py"))
    monkeypatch.setenv("PATH", "/preexisting/path")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    assert captured["env"]["PATH"] == "/preexisting/path"


def test_run_demucs_failure_includes_stdout_in_error(tmp_path, monkeypatch):
    """demucs prints loader errors to stdout — they must reach the RuntimeError."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    captured: dict = {}
    monkeypatch.setattr(
        subprocess, "run",
        _stub_subprocess_run(
            captured,
            returncode=1,
            stdout="Could not load file song.ogg.\nFFmpeg is not installed.",
            stderr="",
        ),
    )

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError) as excinfo:
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    msg = str(excinfo.value)
    assert "code 1" in msg
    assert "FFmpeg is not installed" in msg
    assert "Could not load file" in msg


def test_run_demucs_failure_with_no_output_yields_sentinel(tmp_path, monkeypatch):
    """Empty stdout+stderr should still produce a meaningful error tail."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    captured: dict = {}
    monkeypatch.setattr(
        subprocess, "run",
        _stub_subprocess_run(captured, returncode=1, stdout="", stderr=""),
    )

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError) as excinfo:
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    assert "(no output)" in str(excinfo.value)


# ── _run_demucs torchaudio.save shim bootstrap ──────────────────────────────
# torchaudio>=2.11 routes .save() through save_with_torchcodec, which
# requires torchcodec. _run_demucs spawns the demucs subprocess via
# `python -c <bootstrap>` so that torchaudio.save is monkey-patched to
# soundfile.write before demucs imports. These tests pin the wire-level
# shape of that bootstrap so accidental refactors don't drop the shim.

def test_run_demucs_uses_dash_c_bootstrap_with_extra_paths_arg(tmp_path, monkeypatch):
    """cmd shape: [python, -c, <bootstrap>, <extra_paths_json>, demucs args...]."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    cmd = captured["cmd"]
    # python interpreter, -c flag, bootstrap script, JSON-encoded extra_paths,
    # then the demucs CLI args.
    assert cmd[1] == "-c"
    bootstrap = cmd[2]
    # extra_paths arg is JSON; round-trip parses to a list of strings.
    import json as _json
    parsed = _json.loads(cmd[3])
    assert isinstance(parsed, list) and all(isinstance(p, str) for p in parsed)
    # demucs args follow.
    assert cmd[4:7] == ["-n", "htdemucs_6s", "-o"]
    # Bootstrap reads sys.argv[1] (the JSON), strips it, then runs demucs.
    assert "sys.argv[1]" in bootstrap
    assert "runpy.run_module" in bootstrap


def test_run_demucs_bootstrap_installs_torchaudio_save_shim(tmp_path, monkeypatch):
    """Bootstrap must monkey-patch torchaudio.save -> soundfile.write so the
    demucs subprocess does not depend on torchcodec being importable. This
    test pins the patch wiring so a refactor that drops the shim is caught."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    bootstrap = captured["cmd"][2]
    # Imports the right libs.
    assert "import torchaudio" in bootstrap
    assert "import soundfile" in bootstrap or "soundfile as _sf" in bootstrap
    # Defines a replacement and assigns it.
    assert "def _ta_save" in bootstrap
    assert "_ta.save = _ta_save" in bootstrap
    # Honors channels_first kwarg (so callers passing False are not corrupted).
    assert "channels_first" in bootstrap
    # Soundfile is what actually does the write.
    assert "_sf.write" in bootstrap


def test_run_demucs_bootstrap_runs_demucs_main(tmp_path, monkeypatch):
    """Bootstrap must end by handing control to demucs's __main__ via runpy
    with run_name='__main__', otherwise demucs's CLI argparse never fires."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _stub_subprocess_run(captured))

    full_ogg = tmp_path / "song.ogg"
    full_ogg.write_bytes(b"")
    with pytest.raises(RuntimeError):
        sloppak_convert._run_demucs(full_ogg, tmp_path / "out", model="htdemucs_6s")

    bootstrap = captured["cmd"][2]
    assert "runpy.run_module" in bootstrap
    assert "demucs" in bootstrap
    assert "run_name='__main__'" in bootstrap or 'run_name="__main__"' in bootstrap


# ── _ffmpeg_wav_to_ogg libvorbis → built-in fallback ────────────────────────
# Tester report: ffmpeg builds without --enable-libvorbis emit
# "Unknown encoder 'libvorbis'". The helper retries with ffmpeg's built-in
# `vorbis -strict experimental` so .ogg encoding still works.

def _stub_ffmpeg_run(responses: list, captured_cmds: list):
    """Stub subprocess.run that returns successive SimpleNamespace responses
    and records each cmd invocation. responses are dicts forwarded as kwargs
    to SimpleNamespace (returncode/stdout/stderr)."""
    def fake_run(cmd, capture_output=False, **kwargs):
        captured_cmds.append(list(cmd))
        if not responses:
            raise AssertionError(f"unexpected subprocess.run call: {cmd}")
        resp = responses.pop(0)
        # Side effect: emulate ffmpeg writing the output file on success
        # so the helper's exists()/stat() check passes.
        if resp.get("returncode") == 0 and resp.get("write_output"):
            out_path = cmd[-1]
            from pathlib import Path as _P
            p = _P(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00" * 200)  # > 100-byte size guard
        return SimpleNamespace(
            returncode=resp.get("returncode", 0),
            stdout=resp.get("stdout", b""),
            stderr=resp.get("stderr", b""),
        )
    return fake_run


def test_ffmpeg_wav_to_ogg_libvorbis_first_succeeds(tmp_path, monkeypatch):
    """libvorbis succeeds → no retry, only one subprocess call."""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"\x00" * 200)
    out_ogg = tmp_path / "out.ogg"
    cmds: list = []
    monkeypatch.setattr(
        subprocess, "run",
        _stub_ffmpeg_run([{"returncode": 0, "write_output": True}], cmds),
    )

    r = sloppak_convert._ffmpeg_wav_to_ogg("ffmpeg", wav, out_ogg)

    assert r.returncode == 0
    assert len(cmds) == 1
    assert "libvorbis" in cmds[0]
    assert "experimental" not in cmds[0]


def test_ffmpeg_wav_to_ogg_falls_back_on_unknown_libvorbis(tmp_path, monkeypatch):
    """libvorbis missing → retry with built-in vorbis -strict experimental."""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"\x00" * 200)
    out_ogg = tmp_path / "out.ogg"
    cmds: list = []
    monkeypatch.setattr(
        subprocess, "run",
        _stub_ffmpeg_run(
            [
                {"returncode": 1, "stderr": b"Unknown encoder 'libvorbis'\n"},
                {"returncode": 0, "write_output": True},
            ],
            cmds,
        ),
    )

    r = sloppak_convert._ffmpeg_wav_to_ogg("ffmpeg", wav, out_ogg)

    assert r.returncode == 0
    assert len(cmds) == 2
    assert "libvorbis" in cmds[0]
    # Retry uses the built-in encoder under -strict experimental.
    assert "vorbis" in cmds[1]
    assert "libvorbis" not in cmds[1]
    assert "experimental" in cmds[1]


def test_ffmpeg_wav_to_ogg_does_not_retry_on_unrelated_error(tmp_path, monkeypatch):
    """If ffmpeg fails for a reason other than missing libvorbis, return the
    original failure instead of masking it with a built-in retry."""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"\x00" * 200)
    out_ogg = tmp_path / "out.ogg"
    cmds: list = []
    monkeypatch.setattr(
        subprocess, "run",
        _stub_ffmpeg_run(
            [{"returncode": 1, "stderr": b"No such file or directory\n"}],
            cmds,
        ),
    )

    r = sloppak_convert._ffmpeg_wav_to_ogg("ffmpeg", wav, out_ogg)

    assert r.returncode == 1
    assert b"No such file or directory" in r.stderr
    assert len(cmds) == 1  # no retry


# ── cleanup_stale_temp_dirs (issue topkoa/slopsmith-plugin-sloppak-converter#24) ─


def test_cleanup_stale_temp_dirs_removes_s2p_prefixed_dirs(tmp_path, monkeypatch):
    """Sweep removes every `s2p_*` directory under the temp root while
    leaving unrelated entries (foreign dirs, regular files) untouched."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    leaked = [
        tmp_path / "s2p_extract_abc",
        tmp_path / "s2p_work_xyz",
        tmp_path / "s2p_wem_42",
        tmp_path / "s2p_split_demucs",
        tmp_path / "s2p_split_zip_77",
    ]
    for d in leaked:
        d.mkdir()
        (d / "trash.bin").write_bytes(b"\x00" * 16)

    foreign_dir = tmp_path / "other_app_data"
    foreign_dir.mkdir()
    (foreign_dir / "real.txt").write_text("keep me", encoding="utf-8")

    foreign_file = tmp_path / "s2p_lookalike_file"  # file, not dir — must survive
    foreign_file.write_text("not ours", encoding="utf-8")

    removed = sloppak_convert.cleanup_stale_temp_dirs()
    assert removed == len(leaked)
    for d in leaked:
        assert not d.exists(), f"{d} should have been removed"
    assert foreign_dir.exists() and (foreign_dir / "real.txt").exists()
    assert foreign_file.exists()


def test_cleanup_stale_temp_dirs_respects_min_age(tmp_path, monkeypatch):
    """When min_age_seconds is positive, only dirs older than the threshold
    are removed — protects live conversions whose staging dir mtime is
    current."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    old = tmp_path / "s2p_extract_old"
    old.mkdir()
    fresh = tmp_path / "s2p_extract_fresh"
    fresh.mkdir()

    # Backdate `old` by 2 hours, leave `fresh` at default mtime.
    import os, time
    past = time.time() - 7200
    os.utime(old, (past, past))

    removed = sloppak_convert.cleanup_stale_temp_dirs(min_age_seconds=3600)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_stale_temp_dirs_uses_nested_mtime_as_activity_signal(
    tmp_path, monkeypatch
):
    """A live Demucs job writes to files under nested subdirectories
    (`s2p_split_xxx/model/track/stem.wav`); the *top* dir's mtime is
    stale even though the job is actively writing. The cleanup must
    use the deepest recent mtime in the tree as its activity signal,
    or it would delete in-flight staging dirs of sibling instances."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    live = tmp_path / "s2p_split_active"
    live.mkdir()
    nested = live / "htdemucs_6s" / "song_a"
    nested.mkdir(parents=True)
    (nested / "vocals.wav").write_bytes(b"freshly-written")

    # Backdate every directory in the tree — top dir, intermediate dirs,
    # and the immediate parent of the leaf — so that only the leaf file
    # itself has a recent mtime. A naive `entry.stat().st_mtime` check
    # on the top dir would conclude this staging dir is stale and delete
    # it; the recursive walk must find the recent leaf and preserve it.
    import os, time
    past = time.time() - 7200
    os.utime(live, (past, past))
    os.utime(live / "htdemucs_6s", (past, past))
    os.utime(nested, (past, past))

    removed = sloppak_convert.cleanup_stale_temp_dirs(min_age_seconds=300.0)
    assert removed == 0
    assert live.exists()
    assert (nested / "vocals.wav").exists()


def test_cleanup_stale_temp_dirs_handles_mid_iteration_oserror(
    tmp_path, monkeypatch
):
    """`iterdir()` returns a lazy generator; an `OSError` from the
    underlying `scandir` can fire mid-walk (temp root unmounted, fs
    glitch). The helper must swallow it and return whatever it had
    already removed, not let it bubble out and crash startup."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    good = tmp_path / "s2p_extract_first"
    good.mkdir()

    def _flaky_iterdir(self):
        yield good
        raise OSError("simulated scandir failure mid-iteration")

    monkeypatch.setattr(sloppak_convert.Path, "iterdir", _flaky_iterdir)

    # Must not raise.
    removed = sloppak_convert.cleanup_stale_temp_dirs()
    assert removed == 1
    assert not good.exists()


def test_newest_mtime_within_mid_walk_oserror_preserves_staging_dir(
    tmp_path, monkeypatch
):
    """If `rglob` raises mid-iteration, `_newest_mtime_within` must return
    ``None`` (unknown activity) rather than a partial mtime.  The caller
    then skips deletion, so an in-flight staging dir is not removed even
    when its top-level mtime appears stale."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    stale_dir = tmp_path / "s2p_split_inflight"
    stale_dir.mkdir()

    # Backdate the top dir to look ancient.
    import os, time
    past = time.time() - 7200
    os.utime(stale_dir, (past, past))

    # Patch rglob to raise mid-iteration so _newest_mtime_within cannot
    # determine whether a recent leaf exists.
    original_rglob = sloppak_convert.Path.rglob

    def _failing_rglob(self, pattern):
        if self == stale_dir:
            raise OSError("simulated rglob failure mid-walk")
        return original_rglob(self, pattern)

    monkeypatch.setattr(sloppak_convert.Path, "rglob", _failing_rglob)

    # min_age_seconds > 0 so the age check runs; rglob failure must cause
    # the dir to be skipped (not deleted).
    removed = sloppak_convert.cleanup_stale_temp_dirs(min_age_seconds=300.0)
    assert removed == 0
    assert stale_dir.exists()


def test_cleanup_stale_temp_dirs_returns_zero_when_temp_root_missing(monkeypatch, tmp_path):
    """A non-existent temp root reports 0 removals and doesn't raise."""
    fake_root = tmp_path / "does_not_exist"
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(fake_root))
    assert sloppak_convert.cleanup_stale_temp_dirs() == 0


def test_cleanup_stale_temp_dirs_skips_symlinks(tmp_path, monkeypatch):
    """A symlink whose name happens to start with the s2p_ prefix is left
    alone — we only ever create real directories, so anything else is
    foreign and must not be followed or deleted."""
    monkeypatch.setattr(sloppak_convert.tempfile, "gettempdir", lambda: str(tmp_path))

    target = tmp_path / "important_target"
    target.mkdir()
    (target / "do_not_delete.txt").write_text("safety", encoding="utf-8")

    link = tmp_path / "s2p_looks_like_ours"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform / by this user")

    removed = sloppak_convert.cleanup_stale_temp_dirs()
    assert removed == 0
    assert link.exists()
    assert target.exists() and (target / "do_not_delete.txt").exists()


# ── stem_separation manifest block (slopsmith#357) ─────────────────────────


def _write_minimal_manifest(source_dir):
    """Seed a tiny valid manifest so _rewrite_stems_manifest has something
    to read + rewrite."""
    import yaml
    mf = source_dir / "manifest.yaml"
    mf.write_text(
        yaml.safe_dump({
            "title": "Test",
            "artist": "Test",
            "duration": 1.0,
            "arrangements": [],
            "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}],
        }, sort_keys=False),
        encoding="utf-8",
    )


def test_rewrite_stems_manifest_writes_stem_separation_block(tmp_path):
    """The new `stem_separation` block lands as a top-level manifest key
    when split_sloppak_stems' rewrite path passes it."""
    import yaml
    _write_minimal_manifest(tmp_path)
    new_stems = [
        {"id": "vocals", "file": "stems/vocals.ogg", "default": "on"},
        {"id": "drums",  "file": "stems/drums.ogg",  "default": "on"},
    ]
    sloppak_convert._rewrite_stems_manifest(
        tmp_path, new_stems,
        stem_separation={"engine": "demucs", "model": "htdemucs_6s", "version": "1.0.0"},
    )
    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    assert data["stems"] == new_stems
    assert data["stem_separation"] == {
        "engine": "demucs",
        "model": "htdemucs_6s",
        "version": "1.0.0",
    }


def test_rewrite_stems_manifest_omits_block_when_not_passed(tmp_path):
    """Single-stem / hand-edited paths don't pass stem_separation —
    the manifest stays clean (no surprise key, and any prior stale
    block gets cleared)."""
    import yaml
    _write_minimal_manifest(tmp_path)
    # Seed a stale stem_separation block to prove it gets cleared
    mf = tmp_path / "manifest.yaml"
    seeded = yaml.safe_load(mf.read_text(encoding="utf-8"))
    seeded["stem_separation"] = {"engine": "stale", "model": "old", "version": "0.0.0"}
    mf.write_text(yaml.safe_dump(seeded, sort_keys=False), encoding="utf-8")

    sloppak_convert._rewrite_stems_manifest(
        tmp_path,
        [{"id": "full", "file": "stems/full.ogg", "default": "on"}],
    )
    data = yaml.safe_load(mf.read_text(encoding="utf-8"))
    assert "stem_separation" not in data


def test_stem_separation_constants_are_stable():
    """Pin the constants so a refactor that silently bumps the engine
    name or schema version trips this test instead of shipping a wire
    break to consumers / remote caches."""
    assert sloppak_convert.STEM_SEPARATION_ENGINE == "demucs"
    assert sloppak_convert.STEM_SEPARATION_SCHEMA_VERSION == "1.0.0"
