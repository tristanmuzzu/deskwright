#!/usr/bin/env python3
"""What a click does when the target is covered, and when the toolkit
wants a hover first.

Pure in-process: the pointer backend, the window list and the look
machinery are all stubbed, so nothing moves and nothing is captured.

Claims:
  1. An occlusion refusal names the blocker's id AND geometry, so the next
     call needs no separate list_windows (2026-08-25: three refusals, four
     calls each).
  2. on_occluded:"click_topmost" clicks anyway in the same call and says
     which window received it -- and stays opt-in, because clicking
     whatever is in front is the mistake the guard exists to prevent.
  3. hover_first approaches and settles before clicking, for CEF/Electron
     buttons that ignore a bare click (2026-08-26, ~12 wasted calls).
  4. A click that changed nothing says the coordinates may still be right
     and names hover_first -- instead of only reading as "you missed".

    python3 -m pytest tests/test_click_guards.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu import input as wi
from wcu.capture import _Look
from wcu.errors import ToolError


class FakePointer:
    def __init__(self):
        self.events: list[tuple] = []

    def move_to(self, x, y):
        self.events.append(("move", round(x), round(y)))

    def click(self, x, y, button="left", count=1, settle=0.06):
        self.events.append(("click", round(x), round(y), button, count))

    def drag(self, *a, **k):
        self.events.append(("drag", a, k))


def _w(wid, cls, title="", x=0, y=0, width=800, height=600):
    return {"id": wid, "wm_class": cls, "title": title, "x": x, "y": y,
            "width": width, "height": height, "focused": False,
            "type": "NORMAL", "minimized": False}


@pytest.fixture
def rig(monkeypatch):
    """A desktop where `target` is at the point and `blocker` is over it."""
    target = _w(11, "creative-cloud", "Creative Cloud")
    blocker = _w(22, "creative-cloud", "Error report details",
                 x=300, y=200, width=420, height=180)
    pointer = FakePointer()

    state = {"at": target}          # which window window_at() reports

    monkeypatch.setattr(wi, "_pointer", lambda: pointer)
    monkeypatch.setattr(wi, "list_windows", lambda: [target, blocker])
    monkeypatch.setattr(wi, "_resolve_target", lambda t: target)
    monkeypatch.setattr(wi, "window_at",
                        lambda x, y: {"window": state["at"], "covering": [],
                                      "source": "compositor"})
    monkeypatch.setattr(wi, "_look_before",
                        lambda *a, **k: _Look(False, None, None, None))
    monkeypatch.setattr(wi, "_look", lambda a, result, watching: result)
    monkeypatch.setattr(wi.time, "sleep", lambda s: None)
    return {"pointer": pointer, "target": target, "blocker": blocker,
            "state": state}


# ------------------------------------------------- 1. the refusal informs
def test_occlusion_refusal_carries_id_and_geometry(rig):
    rig["state"]["at"] = rig["blocker"]
    with pytest.raises(ToolError) as e:
        wi.tool_pointer_click({"x": 400, "y": 250, "expect_window": 11})
    msg = str(e.value)
    assert e.value.code == "occluded"
    assert "id 22" in msg
    assert "420x180+300+200" in msg          # the blocker's rectangle
    assert "click_topmost" in msg            # and how to act on it now
    assert rig["pointer"].events == []       # nothing was clicked


# --------------------------------------------------- 2. retarget, opt-in
def test_click_topmost_clicks_anyway_and_says_so(rig):
    rig["state"]["at"] = rig["blocker"]
    out = wi.tool_pointer_click({"x": 400, "y": 250, "expect_window": 11,
                                 "on_occluded": "click_topmost"})
    assert ("click", 400, 250, "left", 1) in rig["pointer"].events
    assert out["retargeted"]["clicked_instead"].startswith("creative-cloud (id 22)")
    assert out["retargeted"]["expected"] == "11"


def test_retarget_is_not_the_default(rig):
    rig["state"]["at"] = rig["blocker"]
    with pytest.raises(ToolError):
        wi.tool_pointer_click({"x": 400, "y": 250, "expect_window": 11})


def test_retarget_does_not_swallow_other_failures(rig, monkeypatch):
    """click_topmost means 'something is in front of my target', not
    'ignore every guard'."""
    def boom(*a, **k):
        raise ToolError("coordinates are off screen", code="off_screen")
    monkeypatch.setattr(wi, "_guard_point", boom)
    with pytest.raises(ToolError) as e:
        wi.tool_pointer_click({"x": 9e9, "y": 9e9, "expect_window": 11,
                               "on_occluded": "click_topmost"})
    assert e.value.code == "off_screen"


def test_on_occluded_rejects_a_typo(rig):
    with pytest.raises(ToolError) as e:
        wi.tool_pointer_click({"x": 1, "y": 1, "on_occluded": "yes"})
    assert e.value.code == "bad_args"


# ----------------------------------------------------------- 3. hover
def test_hover_first_approaches_then_lands(rig):
    out = wi.tool_pointer_click({"x": 400, "y": 250, "hover_first": True})
    moves = [e for e in rig["pointer"].events if e[0] == "move"]
    assert moves[0] == ("move", 412, 262)    # near, first
    assert moves[1] == ("move", 400, 250)    # then exactly on it
    assert ("click", 400, 250, "left", 1) in rig["pointer"].events
    assert "hover" in out["hover_first"]


def test_no_hover_by_default(rig):
    wi.tool_pointer_click({"x": 400, "y": 250})
    assert [e for e in rig["pointer"].events if e[0] == "move"] == []


# ------------------------------------------------------------- 4. hint
def test_nothing_changed_points_at_hover_first(rig, monkeypatch):
    monkeypatch.setattr(wi, "_look", lambda a, result, watching: {
        **result, "look": {"verdict": "NOTHING on screen changed"}})
    out = wi.tool_pointer_click({"x": 400, "y": 250})
    assert "hover_first:true" in out["nothing_changed_hint"]


def test_no_hint_when_the_screen_changed(rig, monkeypatch):
    monkeypatch.setattr(wi, "_look", lambda a, result, watching: {
        **result, "look": {"verdict": "the screen changed, so this landed"}})
    out = wi.tool_pointer_click({"x": 400, "y": 250})
    assert "nothing_changed_hint" not in out


def test_no_hint_when_hover_was_already_tried(rig, monkeypatch):
    """Repeating the advice that was just taken is noise."""
    monkeypatch.setattr(wi, "_look", lambda a, result, watching: {
        **result, "look": {"verdict": "NOTHING on screen changed"}})
    out = wi.tool_pointer_click({"x": 400, "y": 250, "hover_first": True})
    assert "nothing_changed_hint" not in out


# ------------------------------------------- 5. drag: dwell and diagnosis
def test_drag_default_path_is_the_measured_one(rig):
    """dwell_ms defaults to 0 because the current timings landed 5/5 and the
    slower variant landed 4/5 (2026-08-24). The default must stay untouched."""
    wi.tool_pointer_drag({"from_x": 10, "from_y": 10, "to_x": 90, "to_y": 90})
    (_, _args, kwargs), = [e for e in rig["pointer"].events if e[0] == "drag"]
    assert kwargs["dwell_ms"] == 0


def test_drag_dwell_is_passed_through(rig):
    wi.tool_pointer_drag({"from_x": 10, "from_y": 10, "to_x": 90, "to_y": 90,
                          "dwell_ms": 400})
    (_, _args, kwargs), = [e for e in rig["pointer"].events if e[0] == "drag"]
    assert kwargs["dwell_ms"] == 400


def test_failed_drop_does_not_blame_the_timings(rig, monkeypatch):
    monkeypatch.setattr(wi, "_look", lambda a, result, watching: {
        **result, "look": {"verdict": "NOTHING on screen changed"}})
    out = wi.tool_pointer_drag({"from_x": 10, "from_y": 10, "to_x": 90, "to_y": 90})
    hint = out["nothing_changed_hint"]
    assert "not the first thing to suspect" in hint
    assert "input grab" in hint
    assert "dwell_ms:400" in hint


# ----------------------------------------------------------- the halt gate

def _halt_rig(monkeypatch, answers):
    """A halt switch that gives `answers` and then goes silent."""
    from wcu import shell
    from wcu.errors import ToolError

    monkeypatch.setattr(shell, "_HALT_WAS_ANSWERING", False)
    monkeypatch.setattr(shell, "extension_methods", lambda: {"HaltActive"})
    queue = list(answers)

    def fake_gdbus(method, *a, **k):
        if not queue:
            raise ToolError("the extension is not on the bus",
                            code="extension_unavailable")
        return queue.pop(0)

    monkeypatch.setattr(shell, "_gdbus", fake_gdbus)
    return shell


def test_a_bus_hiccup_does_not_halt_the_run(monkeypatch):
    """The shipped default. A fence that fires on its own is the thing this
    project deliberately does not have: the switch is for a human to stop the
    server, not for D-Bus to stop it."""
    monkeypatch.delenv("WCU_HALT_FAIL_CLOSED", raising=False)
    shell = _halt_rig(monkeypatch, ["(false,)"])
    assert shell.halt_active() is False        # it answered: not halted
    assert shell.halt_active() is False        # it went silent: still not


def test_fail_closed_is_available_for_deployments_that_want_it(monkeypatch):
    """Opt-in. `launch_app` can run `gnome-extensions disable
    wcu@wayland-computer-use`, which unowns the bus name and makes every later
    probe fail -- so a switch that HAS answered and then goes silent counts as
    halted under this setting."""
    monkeypatch.setenv("WCU_HALT_FAIL_CLOSED", "1")
    shell = _halt_rig(monkeypatch, ["(false,)"])
    assert shell.halt_active() is False
    assert shell.halt_active() is True


def test_a_switch_that_was_never_there_does_not_block_everything(monkeypatch):
    """Before the logout that loads the extension, nothing can be halted --
    refusing every acted call there would make a fresh install useless. True
    under either setting."""
    from wcu import shell

    monkeypatch.setenv("WCU_HALT_FAIL_CLOSED", "1")
    monkeypatch.setattr(shell, "_HALT_WAS_ANSWERING", False)
    monkeypatch.setattr(shell, "extension_methods", lambda: set())
    assert shell.halt_active() is False
