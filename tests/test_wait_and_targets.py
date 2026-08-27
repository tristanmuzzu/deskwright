#!/usr/bin/env python3
"""wait_for's honest waiting, and picking one window out of several.

Pure in-process: `list_windows` is stubbed, nothing touches D-Bus or the
screen. Everything here exists because of a journal entry.

Claims:
  1. `elapsed` waits a duration and says so -- the honest form of the
     "wait_for text_appears on a string that cannot appear" hack an agent
     used ~10 times in one session (2026-08-26).
  2. A timeout past the ceiling is CLAMPED and reported, not refused: the
     refusal taught callers to chain waits instead (2026-08-25).
  3. Several windows matching one wm_class resolve to the defensible one --
     focused, or the only real window among splashes -- instead of erroring
     and demanding an id the caller could not have (2026-08-25, 2026-08-26).
  4. When there is no defensible pick it still refuses, because silently
     choosing is how input lands in the wrong window.

    python3 -m pytest tests/test_wait_and_targets.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu import shell
from wcu.errors import ToolError


def _win(wid, cls="acrord32.exe", title="Acrobat", *, focused=False,
         type="NORMAL", width=1200, height=800, minimized=False):
    return {"id": wid, "wm_class": cls, "title": title, "focused": focused,
            "type": type, "width": width, "height": height,
            "minimized": minimized, "x": 0, "y": 0}


# ------------------------------------------------------------- 1. elapsed
def test_elapsed_waits_and_reports_it():
    start = time.monotonic()
    out = shell.tool_wait_for({"condition": "elapsed", "timeout": 0.3})
    assert 0.25 <= time.monotonic() - start < 2.0
    assert out["met"] is True
    assert out["waited_seconds"] >= 0.25
    assert "nothing was polled" in out["evidence"]


def test_elapsed_needs_no_target_or_text():
    """The whole point: a duration is not a condition about a window."""
    out = shell.tool_wait_for({"condition": "elapsed", "timeout": 0.2})
    assert out["met"] is True


def test_elapsed_is_offered_in_the_condition_list():
    with pytest.raises(ToolError) as e:
        shell.tool_wait_for({"condition": "nonsense"})
    assert "elapsed" in str(e.value)


# -------------------------------------------------------------- 2. clamp
def test_timeout_past_the_ceiling_is_clamped_not_refused(monkeypatch):
    # A condition that is met on the first poll, so the clamp is observable
    # without actually waiting the ceiling out.
    monkeypatch.setattr(shell, "list_windows", lambda: [_win(1)])
    out = shell.tool_wait_for({"condition": "window_exists",
                               "target": "acrord32.exe", "timeout": 100_000})
    assert out["met"] is True
    assert out["clamped_from"] == 100_000
    assert out["waited_seconds"] <= shell.WAIT_TIMEOUT_MAX_S


def test_a_timeout_that_fits_is_not_reported_as_clamped():
    out = shell.tool_wait_for({"condition": "elapsed", "timeout": 0.2})
    assert "clamped_from" not in out


def test_the_ceiling_stays_inside_a_client_call_timeout():
    """This blocks one MCP call. A wait that outlives the client's own
    timeout fails in a way the agent cannot tell from a broken tool."""
    assert shell.WAIT_TIMEOUT_MAX_S <= 300


def test_timeout_below_the_floor_is_still_refused():
    with pytest.raises(ToolError) as e:
        shell.tool_wait_for({"condition": "elapsed", "timeout": 0.01})
    assert e.value.code == "bad_args"


def test_the_old_two_minute_ceiling_is_gone():
    """150-180s app-launch waits were the reported need (2026-08-25)."""
    assert shell.WAIT_TIMEOUT_MAX_S >= 180


# ------------------------------------------------- 3. defensible pick
def test_focused_window_wins_among_matches(monkeypatch):
    windows = [_win(1, title="Welcome"), _win(2, title="Acrobat", focused=True)]
    monkeypatch.setattr(shell, "list_windows", lambda: windows)
    got = shell._resolve_target("acrord32.exe")
    assert got["id"] == 2
    assert "focused" in got["disambiguated"]["why"]
    assert got["disambiguated"]["passed_over"][0]["id"] == 1


def test_a_hidden_one_by_one_splash_is_passed_over(monkeypatch):
    """The exact 2026-08-26 case: Acrobat spawns a hidden 1x1 dialog
    mid-launch, and the id of the real window is not knowable yet."""
    windows = [_win(1, title="Welcome", type="DIALOG", width=1, height=1),
               _win(2, title="Acrobat Reader")]
    monkeypatch.setattr(shell, "list_windows", lambda: windows)
    got = shell._resolve_target("acrord32.exe")
    assert got["id"] == 2
    assert "zero-size helpers" in got["disambiguated"]["why"]


def test_a_dialog_loses_to_the_normal_window(monkeypatch):
    windows = [_win(1, title="Error report details", type="DIALOG"),
               _win(2, title="Creative Cloud")]
    monkeypatch.setattr(shell, "list_windows", lambda: windows)
    assert shell._resolve_target("acrord32.exe")["id"] == 2


def test_minimized_matches_do_not_win_by_default(monkeypatch):
    windows = [_win(1, minimized=True), _win(2)]
    monkeypatch.setattr(shell, "list_windows", lambda: windows)
    assert shell._resolve_target("acrord32.exe")["id"] == 2


def test_a_single_match_is_returned_untouched(monkeypatch):
    """No disambiguation key when there was nothing to disambiguate."""
    monkeypatch.setattr(shell, "list_windows", lambda: [_win(7)])
    got = shell._resolve_target("acrord32.exe")
    assert got["id"] == 7 and "disambiguated" not in got


# ------------------------------------------------- 4. still refuses
def test_two_equally_real_windows_still_refuse(monkeypatch):
    """Two genuine editor windows: picking one could type into the wrong
    document, which is the failure the guards exist for."""
    windows = [_win(1, "org.gnome.TextEditor", "a.md"),
               _win(2, "org.gnome.TextEditor", "b.md")]
    monkeypatch.setattr(shell, "list_windows", lambda: windows)
    with pytest.raises(ToolError) as e:
        shell._resolve_target("org.gnome.TextEditor")
    assert e.value.code == "bad_args"
    # ...and the refusal now carries what it knows, so the next call can act
    # without a separate list_windows.
    assert "'a.md'" in str(e.value) and "1200x800" in str(e.value)


def test_no_match_still_says_what_is_open(monkeypatch):
    monkeypatch.setattr(shell, "list_windows", lambda: [_win(1)])
    with pytest.raises(ToolError) as e:
        shell._resolve_target("firefox")
    assert e.value.code == "window_not_found"


# ------------------------------- 5. focus failures that can be acted on
def test_focus_failure_says_the_request_was_accepted(monkeypatch):
    """'still not focused' is true and useless. A Wine window accepted
    ActivateWindow, never focused, and the next capture came back 100%
    occluded (2026-08-25) -- several calls spent before anyone knew the
    request had not been refused."""
    target = _win(1, "creative_cloud_set-up.exe", "Creative Cloud")
    monkeypatch.setattr(shell, "list_windows", lambda: [target])
    detail = shell._focus_failure(1, target, target)
    assert "ACCEPTED" in detail
    assert "retrying the same call is unlikely to help" in detail
    # Nothing above it, nothing else focused: the Wine case.
    assert "pointer_click" in detail


def test_focus_failure_names_what_is_stacked_above(monkeypatch):
    target = _win(1, "creative-cloud", "Creative Cloud")
    over = _win(2, "creative-cloud", "Error report details")
    monkeypatch.setattr(shell, "list_windows", lambda: [target, over])
    detail = shell._focus_failure(1, target, target)
    assert "Stacked above it" in detail
    assert "id 2" in detail and "Error report details" in detail
    assert "THEIR pixels" in detail          # why the screenshot looked wrong


def test_focus_failure_ignores_a_window_that_does_not_overlap(monkeypatch):
    target = _win(1, "app", "App", width=100, height=100)
    elsewhere = _win(2, "other", "Other")
    elsewhere.update(x=5000, y=5000)
    monkeypatch.setattr(shell, "list_windows", lambda: [target, elsewhere])
    assert "Stacked above" not in shell._focus_failure(1, target, target)


def test_focus_failure_points_at_unminimize(monkeypatch):
    target = _win(1, "app", "App", minimized=True)
    monkeypatch.setattr(shell, "list_windows", lambda: [target])
    assert "unminimize" in shell._focus_failure(1, target, target)


def test_focus_failure_names_who_holds_focus_instead(monkeypatch):
    target = _win(1, "app", "App")
    thief = _win(2, "modal", "Save changes?", focused=True)
    monkeypatch.setattr(shell, "list_windows", lambda: [target, thief])
    detail = shell._focus_failure(1, target, target)
    assert "Focus is on modal (id 2)" in detail
