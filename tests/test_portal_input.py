"""Unit tests for the portal input backend and the look/capture fixes.

The live path (a real portal consent, absolute motion through a ScreenCast
stream, token reuse) was proven interactively on 2026-08-24 and needs a human
or an agent to approve a dialog; these cover the parts that can regress
without one: token persistence per session, the not-allowed self-heal,
stream-relative coordinate mapping, and the rule that a failed look must never
fail the action it measures.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu.errors import ToolError


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("WCU_HEADLESS", raising=False)
    from wcu import portal_input
    return portal_input


def test_token_round_trip(portal):
    assert portal._load_token() is None
    portal._save_token("tok-1")
    assert portal._load_token() == "tok-1"


def test_token_is_per_session(portal, monkeypatch):
    portal._save_token("primary-token")
    monkeypatch.setenv("WCU_HEADLESS", "1")
    assert portal._load_token() is None, "headless must not inherit the primary consent"
    portal._save_token("headless-token")
    assert portal._load_token() == "headless-token"
    monkeypatch.delenv("WCU_HEADLESS")
    assert portal._load_token() == "primary-token"


def test_forget_drops_only_this_session(portal, monkeypatch):
    portal._save_token("primary-token")
    monkeypatch.setenv("WCU_HEADLESS", "1")
    portal._save_token("headless-token")
    portal._save_token(None, forget=True)
    assert portal._load_token() is None
    monkeypatch.delenv("WCU_HEADLESS")
    assert portal._load_token() == "primary-token"


def test_save_token_ignores_empty(portal):
    portal._save_token("keep-me")
    portal._save_token(None)
    assert portal._load_token() == "keep-me"


def test_stream_for_maps_into_the_containing_stream(portal):
    p = portal.PortalInput()
    # Two monitors side by side: absolute coordinates are per-stream, which is
    # the whole reason the spike's placeholder stream id failed.
    p._streams = [(11, 0, 0, 1280, 720), (22, 1280, 0, 1920, 1080)]
    p._session = "/fake/session"
    assert p._stream_for(100, 100) == (11, 100, 100)
    assert p._stream_for(1380, 200) == (22, 100, 200)


def test_stream_for_refuses_outside_every_stream(portal):
    p = portal.PortalInput()
    p._streams = [(11, 0, 0, 1280, 720)]
    p._session = "/fake/session"
    with pytest.raises(portal.InputError) as e:
        p._stream_for(5000, 5000)
    assert "outside every stream" in str(e.value)


def test_desktop_bounds_spans_all_streams(portal):
    p = portal.PortalInput()
    p._streams = [(11, 0, 0, 1280, 720), (22, 1280, 0, 1920, 1080)]
    p._session = "/fake/session"
    assert p.desktop_bounds() == (0, 0, 3200, 1080)


def test_backend_choice_is_explicit(monkeypatch):
    from wcu import input as wcu_input
    monkeypatch.setattr(wcu_input, "_INPUT_BACKEND", None)
    monkeypatch.setenv("WCU_INPUT_BACKEND", "nonsense")
    with pytest.raises(ToolError) as e:
        wcu_input._input()
    assert e.value.code == "bad_args"
    monkeypatch.setattr(wcu_input, "_INPUT_BACKEND", None)


def test_gestures_are_shared_by_both_backends():
    """One drag implementation, not two -- the timings are measured and must
    not drift apart between the mutter and portal paths."""
    from wcu import portal_input, remote_input
    assert issubclass(remote_input.RemoteInput, remote_input.Gestures)
    assert issubclass(portal_input.PortalInput, remote_input.Gestures)
    assert portal_input.PortalInput.drag is remote_input.Gestures.drag
    assert portal_input.PortalInput.click is remote_input.Gestures.click


def test_failed_look_does_not_fail_the_action():
    """A lost picture must never turn a completed click into an error."""
    from wcu import capture
    from wcu.capture import _Look

    def boom(*_a, **_k):
        raise ToolError("the call returned but no image was written, twice",
                        code="capture_failed")

    original = capture._look_report
    capture._look_report = boom
    try:
        result = capture._look({}, {"detail": "clicked at (10, 10)"},
                               _Look("auto", None, None, None))
    finally:
        capture._look_report = original
    assert result["detail"] == "clicked at (10, 10)"
    assert "capture_failed" in result["look"]["unavailable"]
    assert "action completed" in result["look"]["detail"]
