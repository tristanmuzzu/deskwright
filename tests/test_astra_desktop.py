"""Image fidelity and continuous-stroke contracts without a live desktop."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from deskwright import capture, paths, server, steps
from deskwright import input as desktop_input
from deskwright.errors import ToolError


def test_original_preserves_pixels_and_mcp_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKWRIGHT_IMAGE_PROFILE", "original")
    source = Image.new("RGB", (2400, 1200), "white")
    source.putpixel((2301, 1023), (13, 71, 129))
    path = tmp_path / "fine-detail.png"
    source.save(path)
    out = capture._attach_inline({}, path, {})
    block = server._content_blocks(out)[1]
    actual = Image.open(io.BytesIO(base64.b64decode(block["data"])))
    assert actual.size == source.size
    assert actual.tobytes() == source.tobytes()
    assert block["mimeType"] == "image/png"
    assert block["_meta"] == {"codex/imageDetail": "original"}


def test_profile_override_preserves_legacy_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKWRIGHT_IMAGE_PROFILE", raising=False)
    path = tmp_path / "screen.png"
    Image.new("RGB", (2400, 1200), "white").save(path)
    legacy = capture._attach_inline({}, path, {})
    balanced = capture._attach_inline({}, path, {"image_profile": "balanced"})
    assert legacy["shown"]["dimensions"] == "1568x784"
    assert balanced["shown"]["dimensions"] == "1920x960"
    assert server._content_blocks(legacy)[1]["mimeType"] == "image/jpeg"
    assert "_meta" not in server._content_blocks(legacy)[1]


def test_original_oversize_refuses_lossy_fallback(tmp_path, monkeypatch):
    path = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(path)
    monkeypatch.setattr(capture, "INLINE_MAX_BYTES", 10)
    with pytest.raises(ToolError, match="smaller region"):
        capture._attach_inline({}, path, {"image_profile": "original"})


def test_screenshot_original_crop_coordinates(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_screenshot_region", lambda a: ((200, 100, 2000, 1000), None))
    def shot(path, region, cursor):
        Image.new("RGB", (region[2], region[3]), "white").save(path)
        return True
    monkeypatch.setattr(capture, "_capture", shot)
    out = capture.tool_screenshot({"path": str(tmp_path / "crop.png"),
                                   "image_profile": "original"})
    assert out["shown"]["dimensions"] == "2000x1000"
    assert "200" in out["coordinate_note"] and "100" in out["coordinate_note"]


@pytest.mark.parametrize("change", [
    {"points": [[1, 2]]}, {"points": [[1, 2], [float("nan"), 4]]},
    {"points": [[1, 2], [True, 4]]}, {"points": [[1, 2], [1, 2]]},
    {"duration_ms": 0}, {"duration_ms": float("inf")}, {"duration_ms": True},
    {"button": "invalid"}, {"target": None},
])
def test_path_rejects_malformed_before_input(change):
    with pytest.raises(ToolError):
        paths.validate_path({"points": [[1, 2], [10, 20]], "target": 1, **change})


class Clock:
    now = 0.0
    def time(self):
        return self.now
    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


class Pointer:
    def __init__(self):
        self.events = []
    def move_to(self, x, y):
        self.events.append(("move", x, y))
    def button(self, button, pressed):
        self.events.append((button, pressed))


def test_path_visits_curve_vertices_without_lifting_button():
    pointer, clock = Pointer(), Clock()
    points = [(10, 10), (80, 10), (80, 80), (10, 80)]
    result = paths.execute_path(pointer, points, 1, "left", lambda x, y: None,
                                clock=clock.time, sleep=clock.sleep)
    assert [e for e in pointer.events if e[0] != "move"] == [("left", True), ("left", False)]
    assert all(("move", *p) in pointer.events for p in points)
    assert result["elapsed_ms"] == 1060
    assert result["motion_events"] >= 120


def test_halt_releases_stroke_and_stops_motion():
    pointer, clock = Pointer(), Clock()
    def halt(x, y):
        if clock.now >= 0.2:
            raise ToolError("halted", code="halted")
    with pytest.raises(ToolError, match="halted"):
        paths.execute_path(pointer, [(10, 10), (500, 500)], 2, "left", halt,
                           clock=clock.time, sleep=clock.sleep)
    assert pointer.events[-1] == ("left", False)
    assert clock.now < 0.4
    assert ("move", 500, 500) not in pointer.events


def test_backend_failure_releases_button():
    pointer, clock = Pointer(), Clock()
    def fail(x, y):
        if x > 15:
            raise RuntimeError("backend disconnected")
    pointer.move_to = fail
    with pytest.raises(RuntimeError, match="disconnected"):
        paths.execute_path(pointer, [(10, 10), (100, 100)], 1, "left", lambda x, y: None,
                           clock=clock.time, sleep=clock.sleep)
    assert pointer.events[-1] == ("left", False)


def test_target_bounds_reject_before_opening_input(monkeypatch):
    monkeypatch.setattr(desktop_input, "_resolve_target",
                        lambda x: {"id": 1, "x": 10, "y": 10, "width": 50, "height": 50})
    monkeypatch.setattr(desktop_input, "_pointer", lambda: pytest.fail("input opened"))
    with pytest.raises(ToolError, match="inside the target"):
        desktop_input.tool_pointer_path({"target": 1, "points": [[20, 20], [90, 90]]})


def test_batch_rejects_invalid_later_stroke_before_first_action(monkeypatch):
    monkeypatch.setattr(server, "HANDLERS", {"pointer_click": lambda a: pytest.fail("clicked")})
    with pytest.raises(ToolError, match="finite"):
        steps.tool_do_steps({"steps": [{"do": "click", "x": 20, "y": 20},
            {"do": "path", "target": 1, "points": [[1, 2], [float("nan"), 4]]}]})


def test_health_first_call_uses_live_extension_and_stable_locale(monkeypatch):
    from types import SimpleNamespace

    from deskwright import shell
    monkeypatch.setattr(shell, '_BUS_PROBED', False)
    monkeypatch.setattr(shell, '_EXTENSION_METHODS', None)
    monkeypatch.setattr(shell, 'EXTENSION_UUID', shell.OLD_UUID)
    monkeypatch.setattr(shell, 'BUS_NAME', shell.OLD_BUS)
    monkeypatch.setattr(shell, 'OBJ_PATH', shell.OLD_PATH)
    monkeypatch.setattr(shell, '_introspect_methods', lambda bus, path: {'Ping'})
    def info(cmd, **kwargs):
        assert cmd[-1] == shell.NEW_UUID
        assert kwargs['env']['LC_ALL'] == 'C'
        return SimpleNamespace(stdout='State: ACTIVE\n')
    monkeypatch.setattr(shell.subprocess, 'run', info)
    assert shell._extension_state() == 'ACTIVE'


def test_private_desktop_overrides_inherited_physical_display():
    from deskwright.headless import pin_env
    env = {'DISPLAY': ':0', 'GDK_BACKEND': 'x11', 'QT_QPA_PLATFORM': 'xcb'}
    pin_env({'bus_address':'private-bus', 'wayland_display':'private-wayland',
             'runtime_dir':'/private/runtime', 'name':'test'}, env)
    assert 'DISPLAY' not in env
    assert env['GDK_BACKEND'] == 'wayland'
    assert env['QT_QPA_PLATFORM'] == 'wayland'
    assert env['WAYLAND_DISPLAY'] == 'private-wayland'


def test_nested_private_process_keeps_host_registry(tmp_path, monkeypatch):
    from deskwright import headless
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path/'host-state'))
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/run/user/1000')
    monkeypatch.delenv('DESKWRIGHT_HOST_STATE_HOME', raising=False)
    monkeypatch.delenv('DESKWRIGHT_HOST_RUNTIME_DIR', raising=False)
    env = dict(__import__('os').environ)
    state = {'bus_address':'private', 'wayland_display':'private',
             'runtime_dir':'/run/user/1000/private', 'home':str(tmp_path/'private-home')}
    headless.pin_env(state, env)
    headless.pin_env(state, env)
    assert env['DESKWRIGHT_HOST_STATE_HOME'] == str(tmp_path/'host-state')
    assert env['DESKWRIGHT_HOST_RUNTIME_DIR'] == '/run/user/1000'
    monkeypatch.setenv('DESKWRIGHT_HOST_STATE_HOME',env['DESKWRIGHT_HOST_STATE_HOME'])
    monkeypatch.setenv('XDG_STATE_HOME',env['XDG_STATE_HOME'])
    assert headless._state_dir() == str(tmp_path/'host-state'/'deskwright')


@pytest.mark.parametrize('case,code', [('halt','halted'),('cover','occluded'),('move','widget_moved')])
def test_fast_path_guard_rejects_changed_desktop(case, code):
    import json
    from types import SimpleNamespace

    from deskwright.path_guard import PathGuard
    guard = object.__new__(PathGuard)
    guard.window = {'id':1,'x':0,'y':0,'width':100,'height':100}
    guard.GLib = SimpleNamespace(Variant=lambda *args: None)
    changed = dict(guard.window)
    if case == 'cover':
        changed['id'] = 2
    if case == 'move':
        changed['x'] = 10
    guard._call = lambda method, parameters=None: (case == 'halt') if method == 'HaltActive' else json.dumps({'window':changed})
    with pytest.raises(ToolError) as err:
        guard(50,50)
    assert err.value.code == code


def test_typing_search_is_scoped_to_named_dialog(monkeypatch):
    from types import SimpleNamespace

    from deskwright import atspi
    wrong = SimpleNamespace(get_name=lambda: 'Main window')
    dialog = SimpleNamespace(get_name=lambda: 'New image')
    app = SimpleNamespace(get_name=lambda: 'app', get_child_count=lambda: 2,
                          get_child_at_index=lambda i: [wrong, dialog][i])
    monkeypatch.setattr(atspi, '_find_app', lambda name: app)
    seen = []
    def walk(root, path, *args, **kwargs):
        seen.append((root,path))
    monkeypatch.setattr(atspi, '_walk', walk)
    with pytest.raises(ToolError):
        atspi._locate_text_widget('app', None, window_title='New image')
    assert seen == [(dialog,'app/1')]
    seen.clear()
    with pytest.raises(ToolError):
        atspi._locate_text_widget('app', None, window_title='Gone dialog')
    assert not seen



def test_ui_find_scopes_dialog_and_preserves_index_paths(monkeypatch):
    from types import SimpleNamespace

    from deskwright import atspi
    main = SimpleNamespace(get_name=lambda: 'Main')
    dialog = SimpleNamespace(get_name=lambda: 'Open Image')
    app = SimpleNamespace(get_child_count=lambda: 2,
                          get_child_at_index=lambda i: [main, dialog][i])
    monkeypatch.setattr(atspi, '_find_app', lambda name: app)
    monkeypatch.setattr(atspi, '_app_labels', lambda roots: {id(app): 'gimp'})
    def walk(root, path, depth, limit, collected, cap):
        assert root is dialog and path == 'gimp/1'
        collected.append({'name': 'Location', 'path': path+'/3', 'role': 'text'})
    monkeypatch.setattr(atspi, '_walk', walk)
    result = atspi.tool_ui_find({'app':'gimp','window_title':'Open Image','role':'text'})
    assert result['results'][0]['path'] == 'gimp/1/3'


def test_keyboard_warms_once_without_repeating_requested_key(monkeypatch):
    # This backend uses GLib.Variant; the distro CI job requires this test to run.
    pytest.importorskip("gi", reason="PyGObject (python3-gi) is not installed")
    from deskwright import remote_input
    pointer = remote_input.RemoteInput()
    monkeypatch.setattr(pointer, '_ensure', lambda: ('rd','stream'))
    events = []
    monkeypatch.setattr(pointer, '_call', lambda *a: events.append(a[-1].unpack()))
    monkeypatch.setattr(remote_input.time, 'sleep', lambda seconds: None)
    pointer.keysym(97, True)
    pointer.keysym(97, False)
    pointer.keysym(98, True)
    pointer.keysym(98, False)
    assert events == [(remote_input.KEYSYMS['shift'], True),
                      (remote_input.KEYSYMS['shift'], False),
                      (97, True), (97, False), (98, True), (98, False)]
