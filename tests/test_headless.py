"""Unit tests for wcu/headless.py -- the pure logic only.

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

from wcu import headless  # noqa: E402
from wcu.errors import ToolError  # noqa: E402


def test_status_without_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    report = headless.status()
    assert report["running"] is False
    assert "no headless session" in report["detail"]


def test_status_with_stale_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state_dir = tmp_path / "wayland-computer-use"
    state_dir.mkdir()
    (state_dir / "headless.json").write_text(json.dumps({
        "bus_address": "unix:path=/tmp/does-not-exist",
        "wayland_display": "wayland-wcu", "size": "1280x720",
        # pid 1 is init/systemd, never gnome-shell: alive but the wrong comm,
        # which is exactly the pid-reuse case the comm check exists for.
        "shell_pid": 1, "dbus_pid": 1,
    }))
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
                      "wayland_display": "wayland-wcu"}, env=env)
    assert env == {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/x",
                   "WAYLAND_DISPLAY": "wayland-wcu",
                   "WCU_HEADLESS": "1"}


def test_exec_argv_strips_field_codes(tmp_path):
    from wcu.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Entry]\nName=App\n"
                 "Exec=some-editor --flag %U\n")
    assert _exec_argv(f) == ["some-editor", "--flag"]


def test_exec_argv_ignores_action_sections(tmp_path):
    from wcu.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Action new]\nExec=wrong --one\n"
                 "[Desktop Entry]\nExec=right %f\n")
    assert _exec_argv(f) == ["right"]


def test_exec_argv_refuses_missing_exec(tmp_path):
    from wcu.atspi import _exec_argv
    f = tmp_path / "app.desktop"
    f.write_text("[Desktop Entry]\nName=NoExec\n")
    with pytest.raises(ToolError) as e:
        _exec_argv(f)
    assert e.value.code == "bad_args"


def test_ydotool_refused_when_headless(monkeypatch):
    from wcu.input import _ydotool
    monkeypatch.setenv("WCU_HEADLESS", "1")
    with pytest.raises(ToolError) as e:
        _ydotool("type", "x")
    assert e.value.code == "wrong_session"
    assert "user's screen" in str(e.value)


def test_launch_app_headless_uses_exec_spawn(monkeypatch, tmp_path):
    """In headless mode a desktop_id must NOT go through `gio launch` --
    D-Bus activation on the private session loses the window (2026-08-24)."""
    from wcu import atspi

    f = tmp_path / "org.example.App.desktop"
    f.write_text("[Desktop Entry]\nExec=example-app %U\n")
    monkeypatch.setenv("WCU_HEADLESS", "1")
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
