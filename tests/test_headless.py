"""Unit tests for deskwright/headless.py -- the pure logic only.

The live path (a real gnome-shell --headless on a virtual monitor, clicks
and typing through it) was proven interactively on 2026-08-24 and costs a
~230 MB compositor per run; these tests cover everything that can regress
without one: state handling, env pinning, Exec parsing, and the
ydotool-in-headless refusal.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import headless
from deskwright.errors import ToolError


def _dead_state(display="wayland-deskwright"):
    return {
        "bus_address": "unix:path=/tmp/does-not-exist",
        "wayland_display": display, "size": "1280x720",
        # pid 1 is init/systemd, never gnome-shell: alive but the wrong comm,
        # which is exactly the pid-reuse case the comm check exists for.
        "shell_pid": 1, "dbus_pid": 1,
    }


def test_status_without_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    report = headless.status()
    assert report["running"] is False
    assert "no headless session" in report["detail"]


def test_status_with_stale_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_dir = tmp_path / "deskwright"
    state_dir.mkdir()
    (state_dir / "headless.json").write_text(json.dumps(_dead_state()))
    report = headless.status()
    assert report["running"] is False
    assert report["shell_process"] is False
    assert "stale" in report["detail"]


def test_stop_without_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    report = headless.stop()
    assert report["stopped"] is False


def test_pin_env_sets_all_three():
    env: dict[str, str] = {}
    headless.pin_env({"bus_address": "unix:path=/tmp/x",
                      "wayland_display": "wayland-deskwright"}, env=env)
    assert env == {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/x",
                   "WAYLAND_DISPLAY": "wayland-deskwright",
                   "DESKWRIGHT_HEADLESS": "1",
                   "DESKWRIGHT_HEADLESS_NAME": "default"}


# ---- named sessions (2026-08-27) ----------------------------------------

def test_names_are_validated():
    assert headless.session_name(None) == "default"
    assert headless.session_name("Work") == "work"      # normalised
    # `DESKWRIGHT_SESSION=headless:` names nothing, which means the default one.
    assert headless.session_name("") == "default"
    for bad in ("-lead", "has space", "a" * 33, "slash/es", ".."):
        with pytest.raises(ToolError) as e:
            headless.session_name(bad)
        assert e.value.code == "bad_args"


def test_paths_are_per_name_and_default_keeps_the_old_ones(tmp_path, monkeypatch):
    """A session started before names existed must still be addressable."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert headless.default_display("default") == "wayland-deskwright"
    assert headless.default_display("work") == "wayland-deskwright-work"
    assert headless._state_file("work").endswith("headless-work.json")
    # Two names never share the paths that made two sessions fight.
    assert headless._suffix("default") == ""
    assert headless._suffix("work") == "-work"


def test_legacy_state_file_is_still_read_as_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_dir = tmp_path / "deskwright"
    state_dir.mkdir()
    (state_dir / "headless.json").write_text(json.dumps(_dead_state()))
    assert headless._read_state("default")["wayland_display"] == "wayland-deskwright"
    assert headless.known_names() == ["default"]
    # ...and stopping the default clears BOTH records, so a stale legacy file
    # cannot resurrect a session that was ended.
    headless.stop("default")
    assert not (state_dir / "headless.json").exists()


def test_named_sessions_are_listed_independently(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_dir = tmp_path / "deskwright"
    state_dir.mkdir()
    (state_dir / "headless-work.json").write_text(
        json.dumps(_dead_state("wayland-deskwright-work")))
    (state_dir / "headless-play.json").write_text(
        json.dumps(_dead_state("wayland-deskwright-play")))
    assert headless.known_names() == ["play", "work"]
    report = headless.list_sessions()
    assert report["running"] == 0
    assert {s["name"] for s in report["sessions"]} == {"play", "work"}
    # Stopping one leaves the other's record alone.
    headless.stop("work")
    assert headless.known_names() == ["play"]


def test_pin_env_carries_the_session_name():
    env: dict[str, str] = {}
    headless.pin_env({"bus_address": "unix:path=/tmp/x",
                      "wayland_display": "wayland-deskwright-work",
                      "name": "work"}, env=env)
    assert env["DESKWRIGHT_HEADLESS_NAME"] == "work"


def test_capacity_refuses_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("DESKWRIGHT_HEADLESS_MAX", "1")
    # Hermetic: this test is about the COUNT cap, so the memory floor must not
    # decide it. (It did, on a machine that was genuinely down to 212 MB.)
    monkeypatch.setattr(headless, "_available_mb", lambda: 8000)
    monkeypatch.setattr(headless, "known_names", lambda: ["work"])
    monkeypatch.setattr(headless, "status", lambda name=None: {
        "running": True, "name": headless.session_name(name), "shell_rss_mb": 300})
    with pytest.raises(ToolError) as e:
        headless._check_capacity("second")
    assert "cap is 1" in str(e.value)
    # ...and the session that is ALREADY running under that name is not
    # counted against itself, or a restart could never happen at the cap.
    headless._check_capacity("work")


def test_capacity_refuses_when_memory_is_short(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(headless, "_available_mb", lambda: 120)
    with pytest.raises(ToolError) as e:
        headless._check_capacity("work")
    assert "120 MB available" in str(e.value)


def test_start_lock_is_exclusive_per_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    first, second = headless._StartLock("work"), headless._StartLock("work")
    other = headless._StartLock("play")
    assert first.acquire() is True
    assert second.acquire() is False          # same name: one starter only
    assert other.acquire() is True            # different name: unaffected
    first.release()
    assert second.acquire() is True
    second.release()
    other.release()


def test_start_lock_is_stolen_from_a_dead_holder(tmp_path, monkeypatch):
    """A crashed starter must not wedge the name forever."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lock = headless._StartLock("work")
    with open(lock.path, "w") as f:
        f.write("999999999")                  # a pid that cannot exist
    assert lock.acquire() is True
    lock.release()


def test_log_rotation_keeps_one_generation(tmp_path):
    log = tmp_path / "headless-shell.log"
    log.write_text("x" * 4096)
    headless._rotate_log(str(log), keep_bytes=1024)
    assert (tmp_path / "headless-shell.log.1").exists()
    assert not log.exists()                   # reopened append-mode by caller


def test_exec_argv_strips_field_codes(tmp_path):
    from deskwright.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Entry]\nName=App\n"
                 "Exec=some-editor --flag %U\n")
    assert _exec_argv(f) == ["some-editor", "--flag"]


def test_exec_argv_ignores_action_sections(tmp_path):
    from deskwright.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Action new]\nExec=wrong --one\n"
                 "[Desktop Entry]\nExec=right %f\n")
    assert _exec_argv(f) == ["right"]


def test_exec_argv_refuses_missing_exec(tmp_path):
    from deskwright.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Entry]\nName=NoExec\n")
    with pytest.raises(ToolError) as e:
        _exec_argv(f)
    assert e.value.code == "bad_args"


def test_ydotool_refused_when_headless(monkeypatch):
    from deskwright.input import _ydotool
    monkeypatch.setenv("DESKWRIGHT_HEADLESS", "1")
    with pytest.raises(ToolError) as e:
        _ydotool("type", "x")
    assert e.value.code == "wrong_session"
    assert "user's screen" in str(e.value)


def test_launch_app_headless_uses_exec_spawn(monkeypatch, tmp_path):
    """In headless mode a desktop_id must NOT go through `gio launch` --
    D-Bus activation on the private session loses the window (2026-08-24)."""
    from deskwright import atspi

    f = tmp_path / "org.example.App.desktop"
    f.write_text("[Desktop Entry]\nExec=example-app %U\n")
    monkeypatch.setenv("DESKWRIGHT_HEADLESS", "1")
    monkeypatch.setattr(atspi, "_resolve_desktop_file", lambda _id: f)

    seen: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(atspi.subprocess, "Popen", fake_popen)
    out = atspi.tool_launch_app({"desktop_id": "org.example.App",
                                 "wait_window": False})
    assert seen["argv"] == ["example-app"]
    assert out["via"] == "exec (headless)"


# ------------------------------------------------------- a session's own home

def test_home_env_moves_every_variable_that_matters(tmp_path):
    """HOME alone is not enough. GLib reads the XDG_*_HOME variables
    independently, so a session with a fresh HOME and the user's
    XDG_CONFIG_HOME still restores the user's documents and still paints
    their desktop icons."""
    env = headless._home_env(str(tmp_path / "h"))
    root = str((tmp_path / "h").resolve())
    assert env["HOME"] == root
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "XDG_CACHE_HOME", "XDG_DESKTOP_DIR"):
        assert env[key].startswith(root), (key, env[key])


def test_home_env_creates_the_directories_it_names(tmp_path):
    """gnome-shell will not create them, and a missing XDG_DATA_HOME means no
    extensions directory, which means the session never answers on the bus."""
    env = headless._home_env(str(tmp_path / "h"))
    from pathlib import Path
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DESKTOP_DIR"):
        assert Path(env[key]).is_dir(), key


def test_a_session_home_is_reported_by_status(tmp_path, monkeypatch):
    """pin_env reads it off the status report to point launched apps at the
    same home as the shell. It was dropped by a key whitelist once, and the
    symptom was an app opening the user's last document on a 'clean' desktop."""
    state = {"name": "demo", "bus_address": "unix:path=/tmp/x",
             "wayland_display": "wayland-deskwright-demo", "size": "1280x720",
             "runtime_dir": "/run/user/1000/x", "home": str(tmp_path),
             "shell_pid": 1, "dbus_pid": 2, "started_at": 0.0, "log": "/tmp/l"}
    monkeypatch.setattr(headless, "_read_state", lambda name: state)
    monkeypatch.setattr(headless, "_pid_is", lambda pid, comm: False)
    assert headless.status("demo")["home"] == str(tmp_path)


def test_pin_env_sends_launched_apps_to_the_session_home(tmp_path):
    """Otherwise the shell has the session home and every app launched into it
    has the user's, which is the worst of both."""
    env: dict = {}
    headless.pin_env({"bus_address": "unix:path=/tmp/x",
                      "wayland_display": "wayland-deskwright-demo",
                      "runtime_dir": "/run/user/1000/x",
                      "home": str(tmp_path), "name": "demo"}, env)
    assert env["HOME"] == str(tmp_path.resolve())
    assert env["XDG_DATA_HOME"].startswith(str(tmp_path.resolve()))


def test_a_session_without_a_home_leaves_the_environment_alone(tmp_path):
    env: dict = {}
    headless.pin_env({"bus_address": "unix:path=/tmp/x",
                      "wayland_display": "wayland-deskwright-demo",
                      "runtime_dir": "/run/user/1000/x", "name": "demo"}, env)
    assert "HOME" not in env
