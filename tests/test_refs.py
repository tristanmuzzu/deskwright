#!/usr/bin/env python3
"""Set-of-Mark refs: screen_map numbers widgets, pointer_click/ui_press take
the number back, and every unsafe way of using a ref is refused.

Pure in-process: the ref table is populated directly, the AT-SPI path
resolver is monkeypatched, and the look/pointer machinery is stubbed. Nothing
here touches D-Bus, the compositor, or the screen.

Claims:
  1. register_refs numbers widgets monotonically ACROSS generations and
     replaces the whole table each call -- which is what makes an old ref
     detectably stale rather than silently renumbered.
  2. A stale-generation ref refuses with bad_args telling the caller to take
     a fresh screen_map; a never-issued ref and a non-integer ref refuse too.
  3. Acting on a ref re-verifies widget identity: a widget whose name or role
     changed, or whose path vanished, refuses with widget_moved -- the same
     check ui_press applies to a path.
  4. ref plus x/y (pointer_click) or ref plus path (ui_press) is bad_args.
  5. The happy paths: pointer_click(ref) clicks at the widget's CURRENT
     extents centre under the table's window guard, and ui_press(ref) presses
     the widget with expectations filled in from the table.

    python3 -m pytest tests/test_refs.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import atspi
from deskwright import input as input_mod
from deskwright.capture import _Look
from deskwright.errors import ToolError

NOT_LOOKING = _Look(False, None, None, None)


def widget(name="Save", role="push button", path="app/0/1",
           click_at=(100, 200)):
    return {"name": name, "role": role, "path": path,
            "bounds": {"x": click_at[0] - 40, "y": click_at[1] - 15,
                       "w": 80, "h": 30},
            "click_at": list(click_at)}


class FakeNode:
    """Just enough AT-SPI surface for identity, extents and pressing."""

    def __init__(self, name="Save", role="push button",
                 extents=(60, 185, 80, 30), n_actions=1):
        self._name, self._role = name, role
        self._extents = extents
        self._n_actions = n_actions
        self.pressed: list[int] = []

    def get_name(self):
        return self._name

    def get_role_name(self):
        return self._role

    def get_extents(self, _coord_type):
        x, y, w, h = self._extents
        return SimpleNamespace(x=x, y=y, width=w, height=h)

    def get_parent(self):            # no frame ancestor -> extents trusted
        return None

    def get_component_iface(self):
        return None

    def get_action_iface(self):
        return object() if self._n_actions else None

    def get_n_actions(self):
        return self._n_actions

    def get_localized_name(self, _i):
        return "click"

    def do_action(self, i):
        self.pressed.append(i)
        return True


@pytest.fixture(autouse=True)
def clean_refs():
    """Every test starts with no refs ever issued, and leaves none behind."""
    saved = (dict(atspi._REF_TABLE), atspi._REF_GENERATION, atspi._NEXT_REF)
    atspi._REF_TABLE.clear()
    atspi._REF_GENERATION = 0
    atspi._NEXT_REF = 1
    yield
    atspi._REF_TABLE.clear()
    atspi._REF_TABLE.update(saved[0])
    atspi._REF_GENERATION, atspi._NEXT_REF = saved[1], saved[2]


def code_of(excinfo) -> str:
    return excinfo.value.code


# ---- the table itself -----------------------------------------------------

def test_register_refs_numbers_in_place_and_reports_generation():
    widgets = [widget("Save"), widget("Cancel", path="app/0/2")]
    gen = atspi.register_refs(widgets, "app", window_id=7)
    assert gen == 1
    assert [w["ref"] for w in widgets] == [1, 2]
    assert atspi._REF_TABLE[1]["expect_name"] == "Save"
    assert atspi._REF_TABLE[1]["expect_role"] == "push button"
    assert atspi._REF_TABLE[1]["window_id"] == 7
    assert atspi._REF_TABLE[1]["path"] == "app/0/1"


def test_new_generation_replaces_table_and_keeps_counting():
    atspi.register_refs([widget("Save")], "app", None)
    gen2 = atspi.register_refs([widget("Open"), widget("Close")], "app", None)
    assert gen2 == 2
    # numbering continues -- old numbers are never reissued
    assert sorted(atspi._REF_TABLE) == [2, 3]
    assert 1 not in atspi._REF_TABLE


def test_empty_registration_invalidates_everything():
    atspi.register_refs([widget("Save")], "app", None)
    atspi.register_refs([], None, None)
    with pytest.raises(ToolError) as e:
        atspi.ref_entry(1)
    assert code_of(e) == "bad_args"


# ---- staleness and validation --------------------------------------------

def test_stale_generation_refuses_and_names_the_fix():
    atspi.register_refs([widget("Save")], "app", None)         # ref 1
    atspi.register_refs([widget("Save")], "app", None)         # ref 2
    with pytest.raises(ToolError) as e:
        atspi.ref_entry(1)
    assert code_of(e) == "bad_args"
    assert "older screen_map" in str(e.value)
    assert "fresh screen_map" in str(e.value)
    assert atspi.ref_entry(2)["expect_name"] == "Save"          # current one fine


def test_no_refs_yet():
    with pytest.raises(ToolError) as e:
        atspi.ref_entry(1)
    assert code_of(e) == "bad_args"
    assert "screen_map" in str(e.value)


def test_never_issued_ref():
    atspi.register_refs([widget("Save")], "app", None)
    with pytest.raises(ToolError) as e:
        atspi.ref_entry(99)
    assert code_of(e) == "bad_args"


@pytest.mark.parametrize("bad", ["1", 1.5, True, None, [1]])
def test_ref_must_be_an_integer(bad):
    atspi.register_refs([widget("Save")], "app", None)
    with pytest.raises(ToolError) as e:
        atspi.ref_entry(bad)
    assert code_of(e) == "bad_args"


# ---- identity re-verification (the widget_moved paths) --------------------

def test_resolve_ref_refuses_renamed_widget(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(name="Delete Everything"))
    with pytest.raises(ToolError) as e:
        atspi.resolve_ref(1)
    assert code_of(e) == "widget_moved"
    assert "fresh screen_map" in str(e.value)


def test_resolve_ref_refuses_changed_role(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(name="Save", role="label"))
    with pytest.raises(ToolError) as e:
        atspi.resolve_ref(1)
    assert code_of(e) == "widget_moved"


def test_resolve_ref_refuses_vanished_path(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)

    def gone(_path):
        raise ToolError("no longer resolves", code="widget_missing")
    monkeypatch.setattr(atspi, "_resolve_path", gone)
    with pytest.raises(ToolError) as e:
        atspi.resolve_ref(1)
    assert code_of(e) == "widget_moved"


def test_resolve_ref_refuses_offview_widget_and_points_at_ui_press(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(extents=(0, 0, 0, 0)))
    with pytest.raises(ToolError) as e:
        atspi.resolve_ref(1)
    assert code_of(e) == "widget_moved"
    assert "ui_press" in str(e.value)


def test_resolve_ref_recomputes_click_point_from_live_extents(monkeypatch):
    atspi.register_refs([widget("Save", click_at=(100, 200))], "app", 7)
    # the widget has moved 300px right since the map, identity intact
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(extents=(360, 185, 80, 30)))
    resolved = atspi.resolve_ref(1)
    assert resolved["click_at"] == [400, 200]
    assert resolved["window_id"] == 7


# ---- pointer_click(ref) ---------------------------------------------------

def test_pointer_click_ref_plus_xy_is_bad_args():
    with pytest.raises(ToolError) as e:
        input_mod.tool_pointer_click({"ref": 1, "x": 5, "y": 5})
    assert code_of(e) == "bad_args"
    assert "not both" in str(e.value)


def test_pointer_click_stale_ref_never_reaches_the_pointer(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(input_mod, "_pointer",
                        lambda: pytest.fail("stale ref reached the pointer"))
    with pytest.raises(ToolError) as e:
        input_mod.tool_pointer_click({"ref": 1})
    assert code_of(e) == "bad_args"


def test_pointer_click_widget_moved_never_reaches_the_pointer(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(name="Cancel"))
    monkeypatch.setattr(input_mod, "_pointer",
                        lambda: pytest.fail("moved widget reached the pointer"))
    with pytest.raises(ToolError) as e:
        input_mod.tool_pointer_click({"ref": 1})
    assert code_of(e) == "widget_moved"


def test_pointer_click_ref_happy_path(monkeypatch):
    atspi.register_refs([widget("Save", click_at=(100, 200))], "app", 7)
    monkeypatch.setattr(atspi, "_resolve_path",
                        lambda p: FakeNode(extents=(360, 185, 80, 30)))

    clicks: list[tuple] = []
    guards: list[tuple] = []
    fake_pointer = SimpleNamespace(
        click=lambda x, y, button, count: clicks.append((x, y, button, count)))
    monkeypatch.setattr(input_mod, "_pointer", lambda: fake_pointer)
    monkeypatch.setattr(input_mod, "_guard_point",
                        lambda x, y, expect: guards.append((x, y, expect))
                        or {"expected": "app", "confirmed_by": "fake"})
    monkeypatch.setattr(input_mod, "list_windows", list)
    monkeypatch.setattr(input_mod, "_look_before",
                        lambda a, **kw: NOT_LOOKING)
    monkeypatch.setattr(input_mod, "_look",
                        lambda a, result, watching: result)
    monkeypatch.setattr(input_mod.time, "sleep", lambda s: None)

    result = input_mod.tool_pointer_click({"ref": 1})
    # clicked at the LIVE centre, not the stored one, guarded by the
    # window the table remembered
    assert clicks == [(400, 200, "left", 1)]
    assert guards == [(400, 200, 7)]
    assert result["ref"] == 1
    assert "Save" in result["widget"]


# ---- ui_press(ref) --------------------------------------------------------

def test_ui_press_ref_plus_path_is_bad_args():
    with pytest.raises(ToolError) as e:
        atspi.tool_ui_press({"ref": 1, "path": "app/0/1"})
    assert code_of(e) == "bad_args"


def test_ui_press_stale_ref_is_bad_args():
    atspi.register_refs([widget("Save")], "app", None)
    atspi.register_refs([widget("Save")], "app", None)
    with pytest.raises(ToolError) as e:
        atspi.tool_ui_press({"ref": 1})
    assert code_of(e) == "bad_args"
    assert "fresh screen_map" in str(e.value)


def test_ui_press_ref_refuses_moved_widget(monkeypatch):
    atspi.register_refs([widget("Save")], "app", None)
    node = FakeNode(name="Cancel")
    monkeypatch.setattr(atspi, "_resolve_path", lambda p: node)
    with pytest.raises(ToolError) as e:
        atspi.tool_ui_press({"ref": 1})
    assert code_of(e) == "widget_moved"
    assert node.pressed == []


def test_ui_press_ref_happy_path(monkeypatch):
    atspi.register_refs([widget("Save", path="app/0/1")], "app", None)
    node = FakeNode(name="Save Document")       # substring match, like ui_press
    monkeypatch.setattr(atspi, "_resolve_path", lambda p: node)
    monkeypatch.setattr(atspi, "_look_before", lambda a, **kw: NOT_LOOKING)
    monkeypatch.setattr(atspi, "_look", lambda a, result, watching: result)

    result = atspi.tool_ui_press({"ref": 1})
    assert node.pressed == [0]
    assert result["ref"] == 1
    assert result["path"] == "app/0/1"
    assert result["widget"] == "Save Document"


def test_ui_press_ref_caller_expectations_override_table(monkeypatch):
    """An explicit expectation beats the table's -- and still protects."""
    atspi.register_refs([widget("Save")], "app", None)
    monkeypatch.setattr(atspi, "_resolve_path", lambda p: FakeNode(name="Save"))
    with pytest.raises(ToolError) as e:
        atspi.tool_ui_press({"ref": 1, "expect_name": "Open"})
    assert code_of(e) == "widget_moved"
