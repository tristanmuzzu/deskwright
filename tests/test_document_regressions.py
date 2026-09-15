"""Regressions found while revising a real GTK4 document, September 7."""
from types import SimpleNamespace

import pytest

from deskwright import atspi, capture
from deskwright import input as wi


@pytest.mark.parametrize("replace", [False, True])
def test_unicode_write_preserves_tail_and_character_offset(monkeypatch, replace):
    class Buffer:
        value = "préface "

        def insert_text(self, offset, text, byte_length):
            # The actual AT-SPI contract: character position, byte length.
            inserted = text.encode("utf-8")[:byte_length].decode("utf-8")
            self.value = self.value[:offset] + inserted + self.value[offset:]
            return True

        def get_role_name(self):
            return "text"

    node = Buffer()

    def delete(buffer, start, end):
        buffer.value = buffer.value[:start] + buffer.value[end:]

    api = SimpleNamespace(
        Text=SimpleNamespace(get_character_count=lambda n: len(n.value)),
        EditableText=SimpleNamespace(delete_text=delete),
    )
    monkeypatch.setattr(atspi, "_atspi", lambda: api)
    monkeypatch.setattr(atspi, "_locate_text_widget", lambda *a: (node, "editor/0"))
    monkeypatch.setattr(atspi, "_text_ifaces", lambda n: (n, n))
    monkeypatch.setattr(atspi, "_read_text", lambda n: n.value)
    monkeypatch.setattr(atspi, "_is_focused", lambda n: False)
    monkeypatch.setattr(atspi.time, "sleep", lambda n: None)
    value = "Café — 山 🏔️\nFinal punctuation.\n"
    out = atspi.tool_ui_set_text({"path": "editor/0", "text": value, "replace": replace})
    assert node.value == ("" if replace else "préface ") + value
    assert out["verified"] is True


def test_unreliable_focus_flag_never_injects_navigation_into_document(monkeypatch):
    value = {"text": "Original "}
    events = []

    class Pointer:
        def combo(self, keys):
            events.append(("unexpected_navigation", keys))
            value["text"] += "\t"

        def type_text(self, text, delay):
            events.append(("type", text))
            value["text"] += text

    monkeypatch.setattr(wi, "focus_window", lambda t: {
        "window": {"wm_class": "editor", "title": "Document"}, "detail": "focused"})
    monkeypatch.setattr(wi, "_look_before", lambda *a, **k: capture._Look(False, None, None, None))
    monkeypatch.setattr(wi, "_atspi_app_for_window", lambda w: "editor")
    monkeypatch.setattr(wi, "ensure_widget_focus", lambda *a, **k: {
        "state": "grab_failed", "path": "editor/0"})
    monkeypatch.setattr(wi, "_find_text_widget", lambda *a: value)
    monkeypatch.setattr(wi, "_read_text", lambda n: n["text"])
    monkeypatch.setattr(wi, "_pointer", Pointer)
    monkeypatch.setattr(wi.time, "sleep", lambda n: None)
    out = wi.tool_type_text({"target": 1, "text": "replacement\nsecond line"})
    assert events == [("type", "replacement\nsecond line")]
    assert value["text"] == "Original replacement\nsecond line"
    assert out["verified"] is True


def test_visual_threshold_is_evidence_not_action_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_shot_path", lambda a: (tmp_path / "shot.png", ""))
    monkeypatch.setattr(capture, "_settle", lambda *a: {
        "fingerprint": [2], "settled": True, "frames": 2, "waited_seconds": .1})
    monkeypatch.setattr(capture, "_changed_since", lambda *a: {
        "landed": False, "percent": .253, "strong_cells": 2, "max_delta": 67})
    result = capture._look_report({}, {"verified": True}, capture._Look("auto", None, None, [1]))
    assert result["verified"] is True
    assert result["look"]["visual_change_detected"] is False
    assert "small edits may still have succeeded" in result["look"]["verdict"]
    assert "missed" not in result["look"]["verdict"]
    assert wi._changed_nothing(result) is True


# ---- ui_set_text default semantics (issue #2) ------------------------------
def _set_text_env(monkeypatch, initial):
    """The issue-#2 environment: one editable widget, honest readback."""
    class Buffer:
        value = initial

        def insert_text(self, offset, text, byte_length):
            inserted = text.encode("utf-8")[:byte_length].decode("utf-8")
            self.value = self.value[:offset] + inserted + self.value[offset:]
            return True

        def get_role_name(self):
            return "text"

    node = Buffer()

    def delete(buffer, start, end):
        buffer.value = buffer.value[:start] + buffer.value[end:]

    api = SimpleNamespace(
        Text=SimpleNamespace(get_character_count=lambda n: len(n.value)),
        EditableText=SimpleNamespace(delete_text=delete),
    )
    monkeypatch.setattr(atspi, "_atspi", lambda: api)
    monkeypatch.setattr(atspi, "_locate_text_widget", lambda *a: (node, "editor/0"))
    monkeypatch.setattr(atspi, "_text_ifaces", lambda n: (n, n))
    monkeypatch.setattr(atspi, "_read_text", lambda n: n.value)
    monkeypatch.setattr(atspi, "_is_focused", lambda n: False)
    monkeypatch.setattr(atspi.time, "sleep", lambda n: None)
    return node


def test_set_text_default_replaces_not_appends(monkeypatch):
    """Issue #2's exact repro: two default calls must yield the second value."""
    node = _set_text_env(monkeypatch, "")
    atspi.tool_ui_set_text({"path": "editor/0", "text": "ONE"})
    atspi.tool_ui_set_text({"path": "editor/0", "text": "TWO"})
    assert node.value == "TWO"


def test_set_text_append_is_opt_in(monkeypatch):
    """replace=False keeps the old behaviour for callers who ask for it."""
    node = _set_text_env(monkeypatch, "préface ")
    atspi.tool_ui_set_text({"path": "editor/0", "text": "ONE", "replace": False})
    assert node.value == "préface ONE"


def test_set_text_failed_append_no_longer_verifies(monkeypatch):
    """A doubled append (retry after failure) must FAIL verify, not pass.

    Old check was `text in after` — the substring is present in "ONEONE",
    so a corrupted widget reported verified:True.
    """
    node = _set_text_env(monkeypatch, "ONE")

    def double_append(offset, text, byte_length):
        # a widget that applies the insert twice, as a retried write can
        Buffer = type(node)
        Buffer.insert_text(node, offset, text, byte_length)
        Buffer.insert_text(node, offset, text, byte_length)
        return True

    node.insert_text = double_append
    from deskwright.errors import ToolError
    with pytest.raises(ToolError, match="does not hold what was written"):
        atspi.tool_ui_set_text({"path": "editor/0", "text": "ONE",
                                "replace": False})
