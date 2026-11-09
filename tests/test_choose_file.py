#!/usr/bin/env python3
"""choose_file finds a chooser, types into it, and never answers a follow-up.

No desktop: windows, focus and input are patched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import filechooser, shell
from deskwright import input as inp
from deskwright.errors import ToolError

APP = {"id": 1, "pid": 10, "wm_class": "org.gnome.TextEditor", "title": "Untitled",
       "type": "NORMAL", "transient_for": None}
CHOOSER = {"id": 2, "pid": 10, "wm_class": "org.gnome.TextEditor", "title": "Open File",
           "type": "DIALOG", "transient_for": 1}


def test_recognises_a_titled_dialog():
    w, how = filechooser.find_chooser([APP, CHOOSER])
    assert w["id"] == 2 and "Open File" in how


def test_a_normal_window_titled_open_is_not_a_chooser():
    w, _ = filechooser.find_chooser([dict(APP, title="Open Source Licenses")])
    assert w is None


def test_recognises_a_portal_window(monkeypatch):
    monkeypatch.setattr(filechooser, "_program",
                        lambda pid: "xdg-desktop-portal-gnome" if pid == 99 else "x")
    portal = {"id": 3, "pid": 99, "wm_class": None, "title": "", "type": "NORMAL"}
    w, how = filechooser.find_chooser([APP, portal])
    assert w["id"] == 3 and "portal" in how



@pytest.mark.parametrize("title, want", [
    ("Pick Files", True), ("Open File", True), ("Select Folder", True),
    ("Downloads", False), ("Open", False), ("Project files", False)])
def test_recognises_the_nautilus_hosted_chooser_by_its_title(monkeypatch, title, want):
    """GNOME 50: the portal's chooser is a NORMAL Nautilus window, not transient."""
    monkeypatch.setattr(filechooser, "_program",
                        lambda pid: "nautilus" if pid == 77 else "x")
    files = {"id": 4, "pid": 77, "wm_class": "org.gnome.Nautilus", "title": title,
             "type": "NORMAL", "transient_for": None}
    w, how = filechooser.find_chooser([APP, files])
    assert (w is not None and w["id"] == 4) is want, how

@pytest.fixture
def desk(monkeypatch, tmp_path):
    state = {"windows": [APP, CHOOSER], "keys": [], "typed": [], "close_on_return": True,
             "followup": False}
    monkeypatch.setattr(shell, "list_windows", lambda: list(state["windows"]))
    monkeypatch.setattr(shell, "focus_window", lambda t: {"window": CHOOSER})

    def keys(a):
        state["keys"].append(a["combo"])
        if a["combo"] == "Return":
            if state["followup"]:
                state["windows"].append({"id": 4, "title": "Replace?", "transient_for": 2})
            elif state["close_on_return"]:
                state["windows"] = [APP]
        return {}
    monkeypatch.setattr(inp, "tool_press_keys", keys)
    monkeypatch.setattr(inp, "tool_type_text",
                        lambda a: state["typed"].append(a["text"]) or {"verified": True})
    f = tmp_path / "report.txt"
    f.write_text("x")
    state["file"] = str(f)
    return state


def test_fills_and_confirms(desk):
    out = filechooser.tool_choose_file({"path": desk["file"]})
    assert out["accepted"] is True and desk["typed"] == [desk["file"]]
    assert desk["keys"] == ["ctrl+l", "ctrl+a", "Return"]


def test_reports_but_does_not_answer_a_followup(desk):
    desk["followup"] = True
    out = filechooser.tool_choose_file({"path": desk["file"], "timeout": 1})
    assert out["accepted"] is False and out["follow_up_dialog"]["id"] == 4
    assert desk["keys"].count("Return") == 1


def test_says_when_no_chooser_is_open_and_types_nothing(desk):
    desk["windows"] = [APP]
    out = filechooser.tool_choose_file({"path": desk["file"]})
    assert out["chooser_open"] is False and desk["typed"] == [] and desk["keys"] == []


def test_relative_and_missing_paths_are_refused(desk):
    with pytest.raises(ToolError, match="absolute"):
        filechooser.tool_choose_file({"path": "report.txt"})
    with pytest.raises(ToolError, match="does not exist"):
        filechooser.tool_choose_file({"path": "/nonexistent/x.pdf"})
    # but a save dialog may name a new file
    desk["windows"] = [APP]
    assert filechooser.tool_choose_file({"path": "/tmp/new.pdf",
                                         "must_exist": False})["chooser_open"] is False


def test_presses_the_confirm_button_when_return_only_selects(desk, monkeypatch):
    """Nautilus selects the file on Return and waits for Open."""
    desk["close_on_return"] = False
    pressed = []

    def confirm(chooser, windows):
        pressed.append(chooser["id"])
        desk["windows"] = [APP]
        return "Open"
    monkeypatch.setattr(filechooser, "_press_confirm", confirm)
    out = filechooser.tool_choose_file({"path": desk["file"], "timeout": 3})
    assert out["accepted"] is True and out["confirmed_with"] == "Open"
    assert pressed == [2] and desk["keys"].count("Return") == 1


def test_a_folder_path_never_presses_confirm(desk, monkeypatch, tmp_path):
    desk["close_on_return"] = False
    monkeypatch.setattr(filechooser, "_press_confirm",
                        lambda c, w: pytest.fail("confirm pressed for a folder"))
    out = filechooser.tool_choose_file({"path": str(tmp_path), "timeout": 1.5})
    assert out["chooser_open"] is True and "folder" in out["detail"]


def test_a_followup_dialog_is_reported_before_any_confirm(desk, monkeypatch):
    desk["followup"] = True
    monkeypatch.setattr(filechooser, "_press_confirm",
                        lambda c, w: pytest.fail("confirm pressed over a follow-up"))
    out = filechooser.tool_choose_file({"path": desk["file"], "timeout": 2})
    assert out["follow_up_dialog"]["id"] == 4
