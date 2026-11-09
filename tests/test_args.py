#!/usr/bin/env python3
"""Obvious argument variants are read as meant, and the reading is reported.

Every case here is a refusal seen in a real transcript (2026-08 to 2026-11);
see deskwright/args.py. Pure in-process: no desktop, no D-Bus.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import args as argmod
from deskwright import server, shell
from deskwright.errors import ToolError


def check(tool, given):
    """Validate a copy; return (rewritten args, notes)."""
    given = dict(given)
    notes = server.validate_tool_args(tool, given)
    return given, notes


def test_window_id_is_read_as_window_for_screenshot():
    got, notes = check("screenshot", {"window_id": 42})
    assert got == {"window": 42}
    assert notes == ["window_id was read as window"]


def test_window_is_read_as_target_for_press_keys():
    got, _ = check("press_keys", {"window": "gedit", "combo": "ctrl+s"})
    assert got["target"] == "gedit" and "window" not in got


def test_window_name_resolves_to_id_for_ui_snapshot(monkeypatch):
    monkeypatch.setattr(shell, "_resolve_target", lambda t: {"id": 77, "title": t})
    got, notes = check("ui_snapshot", {"window": "Files"})
    assert got == {"window_id": 77}
    assert "window was read as window_id" in notes
    assert any("resolved to 77" in n for n in notes)


def test_both_names_for_the_window_is_refused():
    with pytest.raises(ToolError, match="both"):
        check("screenshot", {"window": 1, "window_id": 2})


@pytest.mark.parametrize("raw, want", [("false", False), ("False", False), ("true", True)])
def test_string_booleans_for_look(raw, want):
    got, notes = check("pointer_click", {"x": 1, "y": 2, "look": raw})
    assert got["look"] is want
    assert notes


def test_look_enum_strings_are_left_alone():
    got, notes = check("pointer_click", {"x": 1, "y": 2, "look": "window"})
    assert got["look"] == "window" and notes == []


def test_json_string_region_is_parsed():
    got, _ = check("pointer_scroll", {"x": 1, "y": 2, "dy": 3, "look": "region",
                                      "look_at": '{"x": 1, "y": 2, "width": 3, "height": 4}'})
    assert got["look_at"] == {"x": 1, "y": 2, "width": 3, "height": 4}


def test_numeric_strings_become_numbers():
    got, _ = check("hold_key", {"key": "w", "seconds": "4"})
    assert got["seconds"] == 4


def test_digit_string_for_an_integer_or_string_target_stays_a_string():
    # target accepts a title fragment, so "123" is not rewritten here;
    # the resolver already treats digit strings as ids.
    got, notes = check("press_keys", {"target": "123", "combo": "a"})
    assert got["target"] == "123" and notes == []


@pytest.mark.parametrize("direction, amount, want", [
    ("down", "5", {"dy": 5}), ("up", 4, {"dy": -4}),
    ("right", 2, {"dx": 2}), ("left", 1, {"dx": -1})])
def test_scroll_direction_and_amount(direction, amount, want):
    got, notes = check("pointer_scroll", {"x": 1, "y": 2, "direction": direction,
                                          "amount": amount})
    assert {k: got[k] for k in want} == want
    assert "direction" not in got and "amount" not in got
    assert "wheel clicks" in notes[0]


def test_scroll_direction_without_amount_defaults_to_three_clicks():
    got, _ = check("pointer_scroll", {"x": 1, "y": 2, "direction": "down"})
    assert got["dy"] == 3


def test_bad_scroll_direction_is_refused():
    with pytest.raises(ToolError, match="up, down, left or right"):
        check("pointer_scroll", {"x": 1, "y": 2, "direction": "sideways"})


def test_synonyms_seen_in_transcripts():
    got, _ = check("ui_find", {"query": "Save"})
    assert got == {"text": "Save"}
    got, _ = check("press_keys", {"keys": "ctrl+s", "target": 1})
    assert got["combo"] == "ctrl+s"


def test_unknown_argument_names_its_closest_neighbour():
    with pytest.raises(ToolError) as e:
        check("pointer_click", {"x": 1, "y": 2, "clicks": 2})
    text = str(e.value)
    assert "unknown arguments" in text
    assert "did you mean count?" in text or "Valid arguments" in text
    assert "Valid arguments:" in text and "expect_window" in text


def test_invalid_choice_names_the_value_and_choices():
    with pytest.raises(ToolError, match="invalid choices") as e:
        check("ui_observe", {"window_id": 1, "mode": "guess"})
    assert "'guess'" in str(e.value) and "native" in str(e.value)


def test_do_steps_steps_are_normalized_before_validation(monkeypatch):
    from deskwright import steps
    seq = [{"do": "scroll", "x": 1, "y": 2, "direction": "down", "amount": 2},
           {"do": "key", "window": "gedit", "combo": "ctrl+s"}]
    steps._validate_sequence({"steps": seq}, seq)
    assert seq[0]["dy"] == 2 and "direction" not in seq[0]
    assert seq[1]["target"] == "gedit"


def test_scroll_over_limit_explains_the_unit():
    from deskwright import input as inp
    with pytest.raises(ToolError, match="not pixels") as e:
        inp.tool_pointer_scroll({"x": 1, "y": 2, "dy": -611})
    assert "about 12 clicks" in str(e.value)


def test_notes_reach_the_caller_through_the_protocol(monkeypatch):
    from deskwright import session
    replies = []
    monkeypatch.setattr(session, "start_deferred", lambda: None)
    monkeypatch.setattr(server, "_respond", lambda ident, result: replies.append(result))
    monkeypatch.setitem(server.HANDLERS, "screenshot", lambda a: {"seen": a})
    server.handle({"id": 1, "method": "tools/call", "params": {
        "name": "screenshot", "arguments": {"window_id": 5}}})
    text = replies[0]["content"][0]["text"]
    assert '"window": 5' in text
    assert "argument_notes" in text and "window_id was read as window" in text


def test_every_tool_has_at_most_one_window_argument():
    """The aliasing rule assumes one; a second would make it ambiguous."""
    for t in server.TOOL_SCHEMAS:
        props = t["inputSchema"].get("properties", {})
        assert len([n for n in argmod.WINDOW_NAMES if n in props]) <= 1, t["name"]
