#!/usr/bin/env python3
"""In-process tests for launch_app and document addressing -- no desktop.

Everything here runs against wcu.atspi directly, with the AT-SPI bus, the
shell extension and subprocess all faked where a call would otherwise leave
the process. What is being proven is the VALIDATION and ADDRESSING logic:
which argument shapes are refused and with which error code, how a desktop id
resolves to a file, which mechanism confirms a launch, and that the text
tools return the path they resolved -- the contract the e2e test's document
identity fix leans on. Anything that needs a live desktop belongs in
test_e2e_real_task.py, not here.

    python3 -m pytest tests/test_atspi_addressing.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wcu import atspi
from wcu.errors import ToolError


def err(fn, args) -> ToolError:
    with pytest.raises(ToolError) as e:
        fn(args)
    return e.value


# =========================================================================
# launch_app argument validation (raises before any snapshot or subprocess)
# =========================================================================
def test_launch_needs_exactly_one_source() -> None:
    assert err(atspi.tool_launch_app, {}).code == "bad_args"
    assert err(atspi.tool_launch_app,
               {"desktop_id": "x", "command": ["x"]}).code == "bad_args"


def test_command_must_be_an_argv_list() -> None:
    for bad in ("gnome-text-editor", [], ["ok", 3], [""], 42):
        e = err(atspi.tool_launch_app, {"command": bad})
        assert e.code == "bad_args", f"command={bad!r}"


def test_file_must_be_a_string() -> None:
    e = err(atspi.tool_launch_app, {"command": ["x"], "file": ["not", "str"]})
    assert e.code == "bad_args"


def test_timeout_bounds() -> None:
    for bad in (0.1, 500):
        e = err(atspi.tool_launch_app, {"command": ["x"], "timeout": bad})
        assert e.code == "bad_args", f"timeout={bad}"


def test_desktop_id_rejects_paths_and_empties() -> None:
    assert err(atspi.tool_launch_app, {"desktop_id": ""}).code == "bad_args"
    e = err(atspi.tool_launch_app, {"desktop_id": "/etc/passwd"})
    assert e.code == "bad_args"
    assert "bare id" in str(e)


def test_unknown_desktop_id_names_the_file_and_search() -> None:
    e = err(atspi.tool_launch_app,
            {"desktop_id": "no.such.app.wcu-test-9x7"})
    assert e.code == "bad_args"
    assert "no.such.app.wcu-test-9x7.desktop" in str(e)
    assert "applications" in str(e)


# =========================================================================
# desktop id -> file resolution
# =========================================================================
def test_desktop_suffix_is_optional(tmp_path, monkeypatch) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    target = apps / "wcu-test-app.desktop"
    target.write_text("[Desktop Entry]\nType=Application\nName=x\nExec=true\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert atspi._resolve_desktop_file("wcu-test-app") == target
    assert atspi._resolve_desktop_file("wcu-test-app.desktop") == target


# =========================================================================
# launching, with subprocess faked
# =========================================================================
class FakePopen:
    calls: ClassVar[list[list[str]]] = []

    def __init__(self, argv, **_kw) -> None:
        FakePopen.calls.append(list(argv))
        self.pid = 4242


def fake_subprocess(run_result=None):
    """A subprocess stand-in with just what tool_launch_app touches."""
    mod = types.SimpleNamespace()
    mod.DEVNULL = -3
    mod.TimeoutExpired = Exception  # never raised by these fakes
    mod.Popen = FakePopen
    runs: list[list[str]] = []

    def run(argv, **_kw):
        runs.append(list(argv))
        return run_result or types.SimpleNamespace(returncode=0, stderr="",
                                                   stdout="")
    mod.run = run
    mod.run_calls = runs
    return mod


def test_command_launch_without_wait(monkeypatch) -> None:
    FakePopen.calls = []
    monkeypatch.setattr(atspi, "subprocess", fake_subprocess())
    out = atspi.tool_launch_app({"command": ["some-editor", "--new-window"],
                                 "file": "/tmp/x.txt", "wait_window": False})
    assert FakePopen.calls == [["some-editor", "--new-window", "/tmp/x.txt"]]
    assert out["via"] == "command"
    assert out["pid"] == 4242
    assert out["confirmed"] is False


def test_gio_launch_argv(tmp_path, monkeypatch) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    target = apps / "wcu-test-app.desktop"
    target.write_text("[Desktop Entry]\nType=Application\nName=x\nExec=true\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sub = fake_subprocess()
    monkeypatch.setattr(atspi, "subprocess", sub)
    out = atspi.tool_launch_app({"desktop_id": "wcu-test-app",
                                 "file": "/tmp/x.txt", "wait_window": False})
    assert sub.run_calls == [["gio", "launch", str(target), "/tmp/x.txt"]]
    assert out["via"] == "gio launch"
    assert out["confirmed"] is False


def test_gio_failure_is_loud(tmp_path, monkeypatch) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "wcu-broken.desktop").write_text("[Desktop Entry]\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    failed = types.SimpleNamespace(returncode=2, stderr="key file has no Exec",
                                   stdout="")
    monkeypatch.setattr(atspi, "subprocess", fake_subprocess(failed))
    e = err(atspi.tool_launch_app,
            {"desktop_id": "wcu-broken", "wait_window": False})
    assert e.code == "bad_args"
    assert "key file has no Exec" in str(e)


# =========================================================================
# arrival confirmation: window diff, AT-SPI degrade, honest timeout
# =========================================================================
def test_arrival_confirmed_by_new_window_id(monkeypatch) -> None:
    FakePopen.calls = []
    monkeypatch.setattr(atspi, "subprocess", fake_subprocess())
    calls = {"n": 0}

    def windows() -> list[dict]:
        calls["n"] += 1
        base = [{"id": 1, "wm_class": "old", "title": "old"}]
        if calls["n"] >= 2:                    # snapshot sees only the old one
            base.append({"id": 2, "wm_class": "fresh", "title": "fresh"})
        return base

    monkeypatch.setattr(atspi, "list_windows", windows)
    monkeypatch.setattr(atspi, "list_atspi_apps", list)
    out = atspi.tool_launch_app({"command": ["some-editor"], "timeout": 5})
    assert out["confirmed"] is True
    assert out["confirmed_by"] == "window"
    assert out["window"]["id"] == 2


def test_arrival_degrades_to_atspi_bus(monkeypatch) -> None:
    FakePopen.calls = []
    monkeypatch.setattr(atspi, "subprocess", fake_subprocess())

    def no_extension() -> list[dict]:
        raise ToolError("locked", code="extension_unavailable")

    calls = {"n": 0}

    def bus() -> list[dict]:
        calls["n"] += 1
        if calls["n"] >= 2:                    # snapshot first, arrival later
            return [{"name": "some-editor", "pid": 77, "children": 1}]
        return []

    monkeypatch.setattr(atspi, "list_windows", no_extension)
    monkeypatch.setattr(atspi, "list_atspi_apps", bus)
    out = atspi.tool_launch_app({"command": ["some-editor"], "timeout": 5})
    assert out["confirmed"] is True
    assert out["confirmed_by"] == "atspi_bus"
    assert out["app"]["pid"] == 77
    assert "extension" in out["note"]


def test_arrival_timeout_names_what_was_awaited(monkeypatch) -> None:
    FakePopen.calls = []
    monkeypatch.setattr(atspi, "subprocess", fake_subprocess())
    monkeypatch.setattr(atspi, "list_windows",
                        lambda: [{"id": 1, "wm_class": "old", "title": "old"}])
    monkeypatch.setattr(atspi, "list_atspi_apps", list)
    e = err(atspi.tool_launch_app,
            {"command": ["some-editor"], "timeout": 0.6})
    assert e.code == "timeout"
    assert "new window" in str(e)
    assert "some-editor" in str(e)


# =========================================================================
# path addressing: forms, validation, and the returned-path contract
# =========================================================================
class DummyNode:
    def __init__(self, role="text", children=None) -> None:
        self.role = role
        self.children = children or []

    def get_child_at_index(self, i):
        return self.children[i] if 0 <= i < len(self.children) else None

    def get_role_name(self) -> str:
        return self.role


def test_resolve_path_rejects_non_numeric_segments(monkeypatch) -> None:
    monkeypatch.setattr(atspi, "_find_app", lambda name: DummyNode())
    with pytest.raises(ToolError) as e:
        atspi._resolve_path("someapp/zero")
    assert e.value.code == "bad_args"


def test_resolve_path_reports_stale_paths(monkeypatch) -> None:
    monkeypatch.setattr(atspi, "_find_app", lambda name: DummyNode())
    with pytest.raises(ToolError) as e:
        atspi._resolve_path("someapp/7")       # no child 7: the tree moved
    assert e.value.code == "widget_missing"


def test_locate_text_widget_echoes_an_explicit_path(monkeypatch) -> None:
    dummy = DummyNode()
    monkeypatch.setattr(atspi, "_resolve_path", lambda p: dummy)
    node, path = atspi._locate_text_widget("someapp", "someapp/0/1")
    assert node is dummy
    assert path == "someapp/0/1"


def test_find_text_widget_still_returns_a_bare_node(monkeypatch) -> None:
    """input.py's type_text verification passes this straight to _read_text --
    a tuple here would break keystroke verification in a module this change
    must not touch."""
    dummy = DummyNode()
    monkeypatch.setattr(atspi, "_resolve_path", lambda p: dummy)
    assert atspi._find_text_widget("someapp", "someapp/0") is dummy


def test_text_tools_validate_before_touching_the_bus() -> None:
    assert err(atspi.tool_ui_read_text, {}).code == "bad_args"
    assert err(atspi.tool_ui_set_text, {"app": "x"}).code == "bad_args"
    assert err(atspi.tool_ui_set_text, {"text": "x"}).code == "bad_args"
    assert err(atspi.tool_ui_press, {}).code == "bad_args"
    assert err(atspi.tool_ui_press, {"path": "x/0"}).code == "no_expectation"
    assert err(atspi.tool_ui_press,
               {"path": "x/0", "expect_name": "   "}).code == "no_expectation"


# =========================================================================
# scroll-into-view: extents judgement, without a bus
# =========================================================================
class Ext:
    def __init__(self, x, y, w, h) -> None:
        self.x, self.y, self.width, self.height = x, y, w, h


class GeoNode:
    def __init__(self, ext, role="push_button", parent=None,
                 component=None) -> None:
        self._ext = ext
        self._role = role
        self._parent = parent
        self._component = component

    def get_extents(self, _coord_type):
        return self._ext

    def get_role_name(self) -> str:
        return self._role

    def get_parent(self):
        return self._parent

    def get_component_iface(self):
        return self._component


def frame(x=0, y=0, w=800, h=600) -> GeoNode:
    return GeoNode(Ext(x, y, w, h), role="frame")


def test_sane_extents_are_no_problem() -> None:
    node = GeoNode(Ext(100, 100, 40, 20), parent=frame())
    assert atspi._extents_problem(node) is None
    assert atspi._scroll_into_view(node) == {}


def test_degenerate_extents_are_a_problem() -> None:
    node = GeoNode(Ext(0, 0, 0, 0), parent=frame())
    assert "degenerate" in atspi._extents_problem(node)


def test_extents_outside_the_frame_are_a_problem() -> None:
    node = GeoNode(Ext(100, 900, 40, 20), parent=frame())   # below the window
    problem = atspi._extents_problem(node)
    assert problem is not None and "outside" in problem


def test_off_view_without_scrollto_gets_an_honest_note() -> None:
    node = GeoNode(Ext(0, 0, 0, 0), parent=frame(), component=None)
    out = atspi._scroll_into_view(node)
    assert "scrolled" not in out
    assert "off-view" in out["note"]
    assert "pressing anyway" in out["note"]
