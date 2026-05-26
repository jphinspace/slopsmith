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


# ── _maybe_extract_pitch ────────────────────────────────────────────────────
# The pitch path is best-effort and gated on multiple config + filesystem
# conditions. These cover the skip gates + the happy-path write to make
# sure the sloppak ends up with vocal_pitch.json + manifest provenance,
# and that early-exit paths don't crash or partially update the manifest.

import json as _json
import yaml as _yaml


def _make_sloppak_with_vocals(tmp_path):
    """Minimal unpacked-sloppak skeleton: manifest + stems/vocals.ogg."""
    src = tmp_path / "song.sloppak"
    src.mkdir()
    (src / "manifest.yaml").write_text(
        _yaml.safe_dump({"id": "song", "stems": [{"id": "vocals", "file": "stems/vocals.ogg"}]}),
        encoding="utf-8",
    )
    (src / "stems").mkdir()
    (src / "stems" / "vocals.ogg").write_bytes(b"fake-ogg")
    return src


def _patch_pitch_config(monkeypatch, **overrides):
    """Stub _get_pitch_config so tests don't have to spin up a real config.json."""
    base = {"enabled": True, "server_url": "http://stub:7865", "api_key": None}
    base.update(overrides)
    monkeypatch.setattr(sloppak_convert, "_get_pitch_config", lambda: base)
    # Also clear the demucs-server fallback so server_url precedence is deterministic.
    monkeypatch.setattr(sloppak_convert, "_get_demucs_server_url", lambda: None)


def test_maybe_extract_pitch_skips_when_disabled(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch, enabled=False)

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()
    mf = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    assert "vocal_pitch" not in mf
    assert "pitch_extraction" not in mf


def test_maybe_extract_pitch_skips_when_no_lyrics(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)

    out = sloppak_convert._maybe_extract_pitch(
        src, [], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_skips_when_vocals_missing(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    (src / "stems" / "vocals.ogg").unlink()
    _patch_pitch_config(monkeypatch)

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_skips_when_no_server(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    # Empty server_url AND no demucs fallback (the _patch_pitch_config default).
    _patch_pitch_config(monkeypatch, server_url=None)

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_skips_when_remote_returns_empty(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)
    # Stub vocal_pitch.extract_pitch_remote (load_sibling import inside
    # _maybe_extract_pitch resolves to the top-level vocal_pitch module
    # since pyproject pythonpath = ['.', 'lib']).
    import vocal_pitch
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", lambda *a, **kw: [])

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_swallows_remote_failure(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("CREPE server request failed: connection refused")
    import vocal_pitch
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", _boom)

    # Must NOT raise — best-effort contract.
    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_persists_with_allow_nan_false(tmp_path, monkeypatch):
    """vocal_pitch.json must never contain `NaN`/`Infinity` tokens —
    they're non-standard JSON and break strict consumers (browsers'
    JSON.parse rejects them outright). The persistence path uses
    allow_nan=False so a future caller that bypasses the
    extract_pitch_remote filter would surface a ValueError at write
    time rather than poison the on-disk file."""
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)

    import vocal_pitch
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote",
                        lambda *a, **kw: [{"t": float("nan"), "d": 0.5, "midi": 64}])

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    # Best-effort: the ValueError from allow_nan=False is caught + logged
    # as warning, returns False. Crucially, vocal_pitch.json was NOT
    # written with a NaN/Infinity token poisoning future reads.
    assert out is False
    assert not (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_writes_file_and_manifest(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)

    fake_notes = [
        {"t": 0.0, "d": 0.5, "midi": 64},
        {"t": 0.6, "d": 0.4, "midi": 67},
    ]
    import vocal_pitch
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", lambda *a, **kw: fake_notes)

    out = sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.5, "w": "hi"}], src / "stems" / "vocals.ogg",
    )
    assert out is True

    pitch_path = src / "vocal_pitch.json"
    assert pitch_path.exists()
    payload = _json.loads(pitch_path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "notes": fake_notes}

    mf = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    assert mf["vocal_pitch"] == "vocal_pitch.json"
    assert mf["pitch_extraction"] == {
        "engine": vocal_pitch.PITCH_EXTRACTION_ENGINE,
        "model": vocal_pitch.PITCH_EXTRACTION_MODEL,
        "version": vocal_pitch.PITCH_EXTRACTION_SCHEMA_VERSION,
    }


def test_load_lyrics_for_pitch_accepts_minimal_shape(tmp_path):
    p = tmp_path / "lyrics.json"
    p.write_text(_json.dumps([
        {"t": 0.0, "d": 0.5, "w": "hi"},
        {"t": 1.0, "d": 0.3, "w": "world"},
    ]), encoding="utf-8")
    out = sloppak_convert._load_lyrics_for_pitch(p)
    assert out == [
        {"t": 0.0, "d": 0.5, "w": "hi"},
        {"t": 1.0, "d": 0.3, "w": "world"},
    ]


def test_load_lyrics_for_pitch_returns_none_on_missing_file(tmp_path):
    assert sloppak_convert._load_lyrics_for_pitch(tmp_path / "nope.json") is None


def test_load_lyrics_for_pitch_returns_none_on_malformed_json(tmp_path):
    p = tmp_path / "lyrics.json"
    p.write_text("{not json", encoding="utf-8")
    assert sloppak_convert._load_lyrics_for_pitch(p) is None


def test_load_lyrics_for_pitch_filters_entries_missing_t_or_d(tmp_path):
    p = tmp_path / "lyrics.json"
    p.write_text(_json.dumps([
        {"t": 0.0, "d": 0.5, "w": "ok"},
        {"t": 1.0, "w": "missing-d"},      # filtered
        {"d": 0.2, "w": "missing-t"},      # filtered
        "not-a-dict",                      # filtered
    ]), encoding="utf-8")
    out = sloppak_convert._load_lyrics_for_pitch(p)
    assert out == [{"t": 0.0, "d": 0.5, "w": "ok"}]


def test_load_lyrics_for_pitch_returns_none_when_all_entries_filtered(tmp_path):
    p = tmp_path / "lyrics.json"
    p.write_text(_json.dumps([{"w": "no-times"}]), encoding="utf-8")
    assert sloppak_convert._load_lyrics_for_pitch(p) is None


def test_load_lyrics_for_pitch_filters_non_finite_t_or_d(tmp_path):
    """`json.loads` accepts the non-standard `NaN`/`Infinity` literals
    by default, so the isinstance(_, float) check alone would pass them
    through to the /pitch endpoint and trigger a strict-server 4xx (or
    worse — get fed to CREPE and silently corrupt the timing). Filter
    explicitly client-side so non-finite values never leave this loader."""
    # Use python literals directly to bypass json's NaN-permissiveness;
    # mock the loader's file content by passing a dict shape with these
    # values directly via the json round-trip.
    p = tmp_path / "lyrics.json"
    # Python's json.dumps with allow_nan=True (the default) emits these
    # as bare `NaN` / `Infinity` tokens which json.loads round-trips.
    p.write_text(
        '[{"t": 0.0, "d": 0.5, "w": "ok"},'
        ' {"t": NaN, "d": 0.5, "w": "nan-t"},'
        ' {"t": 0.0, "d": Infinity, "w": "inf-d"},'
        ' {"t": -Infinity, "d": 0.5, "w": "neg-inf-t"}]',
        encoding="utf-8",
    )
    out = sloppak_convert._load_lyrics_for_pitch(p)
    assert out == [{"t": 0.0, "d": 0.5, "w": "ok"}]


def test_load_lyrics_for_pitch_filters_non_numeric_t_or_d(tmp_path):
    """The /pitch endpoint rejects non-numeric t/d server-side with a 4xx;
    filter those out client-side so we don't waste a round-trip and so the
    happy path's "got N notes" log line reflects what's actually plausible."""
    p = tmp_path / "lyrics.json"
    p.write_text(_json.dumps([
        {"t": 0.0, "d": 0.5, "w": "ok"},
        {"t": "0.0", "d": 0.5, "w": "string-t"},   # filtered (string)
        {"t": 1.0, "d": None, "w": "none-d"},      # filtered (None)
        {"t": True, "d": 0.5, "w": "bool-t"},      # filtered (bool — int subclass but server rejects)
        {"t": 0.0, "d": False, "w": "bool-d"},     # filtered
        {"t": 2, "d": 1, "w": "int-ok"},           # kept (int IS numeric)
    ]), encoding="utf-8")
    out = sloppak_convert._load_lyrics_for_pitch(p)
    assert out == [
        {"t": 0.0, "d": 0.5, "w": "ok"},
        {"t": 2, "d": 1, "w": "int-ok"},
    ]


def test_maybe_transcribe_lyrics_runs_pitch_when_whisperx_disabled_and_lyrics_exist(tmp_path, monkeypatch):
    """The structural case from Copilot round 6: a user who has
    pitch_extraction.enabled=True but whisperx.enabled=False (or a
    convert that explicitly disables transcription) should still get
    pitch over their existing on-disk lyrics. Previously the
    `if not enabled: return False` short-circuit at the top of
    _maybe_transcribe_lyrics killed the pitch path before it could
    fire."""
    src = _make_sloppak_with_vocals(tmp_path)
    existing = src / "lyrics.json"
    existing.write_text(
        _json.dumps([{"t": 0.0, "d": 0.5, "w": "hi"}]), encoding="utf-8"
    )
    mf = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    mf["lyrics"] = "lyrics.json"
    (src / "manifest.yaml").write_text(_yaml.safe_dump(mf), encoding="utf-8")

    _patch_pitch_config(monkeypatch)
    received = {}
    import vocal_pitch
    def _stub(*args, **kwargs):
        received["lyrics"] = args[1]
        return [{"t": 0.0, "d": 0.5, "midi": 64}]
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", _stub)

    out = sloppak_convert._maybe_transcribe_lyrics(
        src,
        [{"id": "vocals", "file": "stems/vocals.ogg"}],
        # WhisperX explicitly off — the wx_enabled=False path used to
        # return immediately without touching pitch.
        enabled=False,
    )
    assert out is False
    assert received.get("lyrics") == [{"t": 0.0, "d": 0.5, "w": "hi"}]
    assert (src / "vocal_pitch.json").exists()


def test_maybe_transcribe_lyrics_runs_pitch_when_lyrics_already_exist(tmp_path, monkeypatch):
    """When a sloppak ships with lyrics (PSARC xml/sng or hand-authored),
    WhisperX skips — but the pitch path should STILL run since the
    karaoke pitch pre-condition (lyrics + vocals + server) is met."""
    src = _make_sloppak_with_vocals(tmp_path)
    # Add an existing lyrics file declared by the manifest.
    existing = src / "lyrics.json"
    existing.write_text(
        _json.dumps([{"t": 0.0, "d": 0.5, "w": "ohai"}]), encoding="utf-8"
    )
    mf = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    mf["lyrics"] = "lyrics.json"
    (src / "manifest.yaml").write_text(_yaml.safe_dump(mf), encoding="utf-8")

    _patch_pitch_config(monkeypatch)
    # Capture the lyrics arg that pitch receives so we can confirm it
    # got the existing-on-disk lyrics, not an empty list or something.
    received = {}
    import vocal_pitch
    def _stub(*args, **kwargs):
        # args = (vocals_path, lyrics, server_url); we capture lyrics.
        received["lyrics"] = args[1]
        return [{"t": 0.0, "d": 0.5, "midi": 64}]
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", _stub)

    out = sloppak_convert._maybe_transcribe_lyrics(
        src,
        [{"id": "vocals", "file": "stems/vocals.ogg"}],
        enabled=True,
    )
    # Transcription itself did NOT run — the function returns False.
    assert out is False
    # But pitch DID run, with the on-disk lyrics.
    assert received.get("lyrics") == [{"t": 0.0, "d": 0.5, "w": "ohai"}]
    # And vocal_pitch.json got written.
    assert (src / "vocal_pitch.json").exists()


def test_maybe_extract_pitch_emits_progress_within_slice(tmp_path, monkeypatch):
    src = _make_sloppak_with_vocals(tmp_path)
    _patch_pitch_config(monkeypatch)

    # Have the stub call its own progress_cb at 0.5 to verify scaling
    # into the caller's [base_frac, base_frac+span_frac] slice.
    def _stub_extract(*args, **kwargs):
        cb = kwargs["progress_cb"]
        cb(0.0, "pitch", "start")
        cb(0.5, "pitch", "mid")
        return [{"t": 0.0, "d": 0.1, "midi": 60}]
    import vocal_pitch
    monkeypatch.setattr(vocal_pitch, "extract_pitch_remote", _stub_extract)

    fracs: list[float] = []
    def _capture(f, stage, msg):
        fracs.append(round(f, 4))
    sloppak_convert._maybe_extract_pitch(
        src, [{"t": 0.0, "d": 0.1, "w": "x"}], src / "stems" / "vocals.ogg",
    # Reserve [0.6, 0.8] of the overall progress space for this call.
        progress_cb=_capture, base_frac=0.6, span_frac=0.2,
    )

    # Strictly monotonic; first scaled tick at 0.60 (0.0 → base), then
    # 0.70 (0.5 mid → base + span/2), then the terminal 0.80 flush.
    assert fracs[0] == 0.6
    assert 0.7 in fracs
    assert fracs[-1] == 0.8
