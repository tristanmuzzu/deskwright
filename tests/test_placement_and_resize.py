#!/usr/bin/env python3
"""launch_app monitor placement, private-desktop size, desktop_resize guards.

No desktop: the window list, monitors and headless lifecycle are patched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import atspi, headless, resize, shell
from deskwright import input as inp
from deskwright.errors import ToolError

MONS = [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True},
        {"index": 1, "x": 1920, "y": 0, "width": 1280, "height": 720, "primary": False}]


@pytest.fixture
def desk(monkeypatch):
    windows = {}
    monkeypatch.setattr(inp, "monitors", lambda: MONS)
    monkeypatch.setattr(shell, "list_windows", lambda: list(windows.values()))

    def manage(a):
        w = windows[a["target"]]
        w.update(x=a["x"], y=a["y"], width=a["width"], height=a["height"])
        return {"ok": True}
    monkeypatch.setattr(shell, "tool_window_manage", manage)
    return windows


def test_window_is_moved_and_fitted_onto_the_named_monitor(desk):
    desk[5] = {"id": 5, "x": 100, "y": 100, "width": 1600, "height": 900}
    out = atspi.place_on_monitor(dict(desk[5]), 1)
    assert out["moved"] is True
    w = out["window"]
    assert (w["x"], w["y"], w["width"], w["height"]) == (1920, 0, 1280, 720)


def test_window_already_there_is_left_alone(desk):
    desk[5] = {"id": 5, "x": 2000, "y": 10, "width": 400, "height": 300}
    assert atspi.place_on_monitor(dict(desk[5]), 1)["moved"] is False


def test_bad_monitor_index_is_refused_before_launch(desk):
    with pytest.raises(ToolError, match=r"0\.\.1"):
        atspi.tool_launch_app({"command": ["true"], "monitor": 5})


def test_monitor_needs_wait_window(desk):
    with pytest.raises(ToolError, match="wait_window"):
        atspi.tool_launch_app({"command": ["true"], "monitor": 0, "wait_window": False})


@pytest.mark.parametrize("raw, want", [("1920x1080", "1920x1080"), ("", "1280x720"),
                                       ("99999x2", "1280x720"), ("big", "1280x720")])
def test_headless_size_setting(monkeypatch, raw, want):
    monkeypatch.setenv("DESKWRIGHT_HEADLESS_SIZE", raw)
    assert headless.default_size() == want


def test_resize_refused_on_the_users_desktop(monkeypatch):
    monkeypatch.delenv("DESKWRIGHT_HEADLESS", raising=False)
    with pytest.raises(ToolError) as e:
        resize.tool_desktop_resize({"width": 1920, "height": 1080})
    assert e.value.code == "wrong_session"


def test_resize_refused_while_windows_are_open(monkeypatch, desk):
    monkeypatch.setenv("DESKWRIGHT_HEADLESS", "1")
    monkeypatch.setenv("DESKWRIGHT_HEADLESS_NAME", "t")
    monkeypatch.setattr(headless, "status", lambda n: {"size": "1280x720"})
    monkeypatch.setattr(headless, "stop", lambda n: pytest.fail("stopped with windows open"))
    desk[5] = {"id": 5, "wm_class": "gimp", "type": "NORMAL"}
    with pytest.raises(ToolError, match="gimp"):
        resize.tool_desktop_resize({"width": 1920, "height": 1080})


def test_resize_restarts_and_asks_for_a_new_worker(monkeypatch, desk):
    calls = []
    monkeypatch.setenv("DESKWRIGHT_HEADLESS", "1")
    monkeypatch.setenv("DESKWRIGHT_HEADLESS_NAME", "t")
    monkeypatch.setattr(headless, "status", lambda n: {"size": "1280x720", "home": None})
    monkeypatch.setattr(headless, "stop", lambda n: calls.append(("stop", n)))
    monkeypatch.setattr(headless, "start", lambda **k: calls.append(("start", k)) or
                        {"size": k["size"], "bus_address": "x", "wayland_display": "w"})
    monkeypatch.setattr(headless, "pin_env", lambda st: calls.append(("pin", st["size"])))
    monkeypatch.setattr(resize, "RESTART_WORKER", False)
    out = resize.tool_desktop_resize({"width": 1920, "height": 1080})
    assert out["restarted"] and out["size"] == "1920x1080"
    assert [c[0] for c in calls] == ["stop", "start", "pin"]
    assert resize.RESTART_WORKER is True


def test_resize_bounds(monkeypatch):
    monkeypatch.setenv("DESKWRIGHT_HEADLESS", "1")
    with pytest.raises(ToolError, match="width"):
        resize.tool_desktop_resize({"width": 100, "height": 720})


def test_watchable_binary_skips_wrappers():
    assert atspi._watchable_binary(["/snap/bin/telegram-desktop", "%u"]) == "telegram-desktop"
    assert atspi._watchable_binary(["env", "FOO=1", "app"]) is None
    assert atspi._watchable_binary(["flatpak", "run", "x"]) is None


def test_pids_running_finds_this_interpreter():
    import os
    import sys as _sys
    assert os.getpid() in atspi._pids_running(os.path.basename(_sys.executable)) or \
        os.getpid() in atspi._pids_running("python3")


def test_gio_launch_that_dies_fails_fast(monkeypatch, tmp_path):
    from types import SimpleNamespace
    desktop = tmp_path / "x.desktop"
    desktop.write_text("[Desktop Entry]\nExec=/snap/bin/telegram-desktop %u\n")
    monkeypatch.delenv("DESKWRIGHT_HEADLESS", raising=False)
    monkeypatch.setattr(atspi, "_resolve_desktop_file", lambda d: desktop)
    monkeypatch.setattr(atspi, "list_windows", lambda: [])
    monkeypatch.setattr(atspi, "list_atspi_apps", lambda: [])
    monkeypatch.setattr(atspi.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(atspi, "LAUNCH_POLL_S", 0.3)
    seq = iter([set(), {4242}, {4242}, {4242}] + [set()] * 50)
    monkeypatch.setattr(atspi, "_pids_running", lambda name: next(seq))
    import time
    t0 = time.monotonic()
    with pytest.raises(ToolError, match="exited before any window") as e:
        atspi.tool_launch_app({"desktop_id": "x", "timeout": 30})
    assert time.monotonic() - t0 < 5
    assert e.value.details["exited_pids"] == [4242]
