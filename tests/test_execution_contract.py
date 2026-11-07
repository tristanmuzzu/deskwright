"""Outcome, coordinate and cancellation regressions. No desktop input."""
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from test_steps_validation import Harness

from deskwright import capture, observations, server, shell, steps
from deskwright.code_runtime import GLOBALS, tool_desktop_exec
from deskwright.errors import ToolError
from deskwright.execution import CURRENT, Execution, clock, execute


def test_unmet_wait_stops_before_return_key():
    with Harness() as h:
        server.HANDLERS['wait_for'] = lambda a: {'met': False}
        out = steps.tool_do_steps({'look': False, 'steps': [
            {'do': 'wait_for', 'condition': 'window_exists', 'target': 'x', 'timeout': .2},
            {'do': 'key', 'target': 'x', 'combo': 'Return'}]})
        assert not out['all_ok'] and out['remaining_steps'] == 1
        assert h.calls == []


def test_partial_input_never_retried():
    with Harness():
        calls = []
        def partial(args):
            calls.append(args)
            raise ToolError('partly typed', code='input_backend_failed', action_status='partial')
        server.HANDLERS['type_text'] = partial
        out = steps.tool_do_steps({'look': False, 'steps': [
            {'do': 'type', 'target': 'x', 'text': 'value', 'retry': {'attempts': 3}}]})
        assert len(calls) == 1
        assert out['results'][0]['action_status'] == 'partial'


def test_window_absence_and_selection_use_trimmed_names(monkeypatch):
    w = {'id': 1, 'wm_class': 'gimp', 'title': 'Drawing', 'focused': True}
    monkeypatch.setattr(shell, 'list_windows', lambda: [w])
    assert shell._resolve_target(' gimp ')['id'] == 1
    assert shell.tool_wait_for({'condition': 'window_gone', 'target': ' gimp ', 'timeout': .2})['met'] is False


def test_failed_clipboard_read_is_not_change(monkeypatch):
    values = iter(['value', None, None, None])
    monkeypatch.setattr(shell, '_read_clipboard_now', lambda: next(values, None))
    assert not shell.tool_wait_for({'condition': 'clipboard_changed', 'timeout': .2})['met']


def test_zero_settle_is_forwarded(tmp_path, monkeypatch):
    args = []
    monkeypatch.setattr(capture, '_shot_path', lambda a: (tmp_path/'unused', True))
    def settle(p, r, n):
        args.append(n)
        raise ToolError('fixture', code='capture_failed')
    monkeypatch.setattr(capture, '_settle', settle)
    capture._look({'settle_max_s': 0}, {}, capture._Look('screen', None, None, None))
    assert args == [0]


def test_crop_mapping_uses_both_actual_dimensions(monkeypatch):
    monkeypatch.setattr(observations, 'geometry', lambda: [(1, 0, 0, 1920, 1080)])
    out = {}
    observations.register(out, (71, 29), (1001, 701), (333, 233))
    mapped = observations.map_input('pointer_click', {
        'observation_id': out['observation']['id'], 'x': 111, 'y': 100})
    assert mapped['x'] == pytest.approx(71 + 111 * 1001 / 333)
    assert mapped['y'] == pytest.approx(29 + 100 * 701 / 233)
    monkeypatch.setattr(observations, 'geometry', lambda: [(1, 10, 0, 1920, 1080)])
    with pytest.raises(ToolError, match='geometry'):
        observations.map_input('pointer_click', {'observation_id': out['observation']['id'], 'x': 1, 'y': 1})


def test_cross_session_and_unknown_observations_refused(monkeypatch):
    monkeypatch.setattr(observations, 'geometry', lambda: [])
    out = {}
    observations.register(out, (0, 0), (100, 100), (100, 100))
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'different-desktop')
    for key in [out['observation']['id'], 'missing']:
        with pytest.raises(ToolError) as e:
            observations.map_input('pointer_move', {'observation_id': key, 'x': 0, 'y': 0})
        assert e.value.code == 'stale_observation'


def test_halt_checked_between_primitives(monkeypatch):
    calls = []
    token = CURRENT.set(Execution(clock()+10))
    try:
        monkeypatch.setattr(shell, 'halt_active', lambda: bool(calls))
        execute('press_keys', {}, handler=lambda a: calls.append(a))
        with pytest.raises(ToolError, match='halt'):
            execute('press_keys', {}, handler=lambda a: calls.append(a))
        assert len(calls) == 1
    finally:
        CURRENT.reset(token)


def test_code_state_survives_exception_and_reset():
    assert tool_desktop_exec({'reset': True, 'code': 'value = 7'})['all_ok']
    assert not tool_desktop_exec({'code': 'value += 1\nraise ValueError("fixture")'})['all_ok']
    assert tool_desktop_exec({'code': 'log(value)'})['output'] == ['8']
    tool_desktop_exec({'reset': True, 'code': 'new_value = 1'})
    assert 'value' not in GLOBALS


def test_query_helper_failure_stops_before_input(monkeypatch):
    calls = []
    def query(args):
        calls.append(args)
        raise ToolError('ambiguous target', code='ambiguous_control', action_status='not_started')
    monkeypatch.setitem(server.HANDLERS, 'ui_query', query)
    monkeypatch.setitem(server.HANDLERS, 'press_keys', lambda a: pytest.fail('input after failed query'))
    out = tool_desktop_exec({'reset': True, 'code':
        "desktop.query(42, save={'role':'button','name':'Save'})\n"
        "desktop.key('Enter', target=42)"})
    assert out['error']['code'] == 'ambiguous_control'
    assert len(calls) == 1 and calls[0]['controls']['save']['name'] == 'Save'


def test_code_calls_use_shared_executor():
    with patch('deskwright.code_runtime.execute', return_value={'ok': True}) as run:
        assert tool_desktop_exec({'code': 'desktop.key("Return", target=42)'})['all_ok']
        assert run.call_args.args == ('press_keys', {'combo': 'Return', 'target': 42,
                                                    'observation_mode': 'compact'})


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
def test_supervisor_stops_infinite_python_and_resets(tmp_path, cause):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DESKWRIGHT_ENABLE_EXEC': '1', 'DESKWRIGHT_SESSION': 'primary',
           'XDG_RUNTIME_DIR': str(tmp_path)}
    env.pop('DESKWRIGHT_HEADLESS_LAZY', None)
    # Use a fixture worker to exercise the real supervisor, without contacting D-Bus.
    fixture = tmp_path/'fixture.py'
    fixture.write_text('''import sys, time
from deskwright import server
server.halt_active = lambda: False
from deskwright import shell
shell.halt_active = lambda: False
from deskwright.worker import main
main()
''')
    # The supervisor's subprocess launch is swapped only to inject harmless fixtures.
    entry = tmp_path/'entry.py'
    entry.write_text(f'''import sys
from deskwright import supervisor
original = supervisor.subprocess.Popen
def worker(command, **kwargs):
    return original([sys.executable, {str(fixture)!r}], **kwargs)
supervisor.subprocess.Popen = worker
if {cause!r} == "deadline":
    real_clock = supervisor.clock
    supervisor.clock = lambda: real_clock() * 120
supervisor.serve()
''')
    env['PYTHONPATH'] = str(root)
    proc = subprocess.Popen([sys.executable, str(entry)], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def send(value):
        proc.stdin.write(json.dumps(value)+'\n')
        proc.stdin.flush()
    def read():
        assert select.select([proc.stdout], [], [], 5)[0], 'supervisor stopped responding'
        return json.loads(proc.stdout.readline())
    try:
        send({'id': 1, 'method': 'tools/call', 'params': {'name': 'desktop_exec',
              'arguments': {'code': 'value = 7\nwhile True: pass'}}})
        time.sleep(.3)
        if cause == 'cancel':
            send({'method': 'notifications/cancelled', 'params': {'requestId': 1}})
        failed = read()
        assert failed['result']['isError']
        assert 'session_reset' in failed['result']['content'][0]['text']
        send({'id': 2, 'method': 'tools/call', 'params': {'name': 'desktop_exec',
              'arguments': {'code': 'log("value" in globals())'}}})
        out = read()
        assert json.loads(out['result']['content'][0]['text'])['output'] == ['false']
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_deadline_after_suspend_prevents_next_input(monkeypatch):
    from deskwright import execution
    now = [100.0]
    monkeypatch.setattr(execution, 'clock', lambda: now[0])
    monkeypatch.setattr(shell, 'halt_active', lambda: False)
    token = CURRENT.set(Execution(160.0))
    calls = []
    try:
        execute('press_keys', {}, handler=lambda a: calls.append(a))
        now[0] = 300.0  # BOOTTIME advanced across suspend.
        with pytest.raises(ToolError, match='deadline'):
            execute('press_keys', {}, handler=lambda a: calls.append(a))
        assert len(calls) == 1
    finally:
        CURRENT.reset(token)


def test_crop_mapping_preserves_drag_argument_contract(monkeypatch):
    monkeypatch.setattr(observations, 'geometry', lambda: [])
    out = {}
    observations.register(out, (20, 40), (200, 400), (100, 100))
    mapped = observations.map_input('pointer_drag', {
        'observation_id': out['observation']['id'],
        'from_x': 10, 'from_y': 10, 'to_x': 50, 'to_y': 50})
    assert mapped == {'from_x': 40, 'from_y': 80, 'to_x': 120, 'to_y': 240}


def test_supervised_raw_uinput_refused_before_sending(monkeypatch):
    from deskwright import input as wi
    token = CURRENT.set(Execution(clock()+10))
    try:
        with patch.object(wi.subprocess, 'run') as run:
            with pytest.raises(ToolError) as e:
                wi._ydotool('key', '42:1')
            assert e.value.action_status == 'not_started'
            run.assert_not_called()
    finally:
        CURRENT.reset(token)


def test_health_uses_live_extension_when_status_cli_is_unknown(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(server, '_extension_state', lambda: 'unknown')
    monkeypatch.setattr(server, '_extension_diagnosis', lambda: pytest.fail('working extension needs no logout diagnosis'))
    monkeypatch.setattr(server, 'list_windows', lambda: [])
    monkeypatch.setattr(server, 'list_atspi_apps', lambda: [1])
    monkeypatch.setattr(server, 'extension_methods', lambda: {'Pointer','WindowAt','ScreenshotArea','ScreenshotWindow'})
    monkeypatch.setattr(server, 'keyboard_layouts', lambda: ['us'])
    monkeypatch.setattr(server, 'layout_hazard', lambda: '')
    monkeypatch.setattr(server, '_input', lambda: SimpleNamespace(shared=lambda: SimpleNamespace(desktop_bounds=lambda: (0,0,1280,800))))
    monkeypatch.setattr(server.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=''))
    out = server.tool_health({})
    assert out['extension'] == 'ACTIVE' and out['extension_cli_state'] == 'unknown'
    assert out['verdict'].startswith('READY')
