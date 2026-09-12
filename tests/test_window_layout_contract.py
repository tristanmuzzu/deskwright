"""Acceptance tests for the proposed narrow window capability, no desktop IO."""
import pytest

from deskwright import server, shell
from deskwright.errors import ToolError
from deskwright.execution import CURRENT, Execution, clock, execute


@pytest.mark.parametrize('action', ['close', ' close ', 'Close', 'launch', '', None, 'future_action'])
def test_layout_rejects_non_layout_actions_before_dispatch(monkeypatch, action):
    sent = []
    monkeypatch.setattr(shell, 'tool_window_manage', lambda args: sent.append(args))
    with pytest.raises(ToolError) as error:
        shell.tool_window_layout({'action': action, 'target': 1})
    assert error.value.code == 'bad_args'
    assert sent == []


@pytest.mark.parametrize('action', ['move_resize', 'minimize', 'unminimize', 'maximize',
                                   'unmaximize', 'workspace', 'above'])
def test_layout_uses_existing_window_implementation(monkeypatch, action):
    captured = []
    result = {'action': action, 'window': {'id': 123}}
    def manage(args):
        captured.append(args)
        return result
    monkeypatch.setattr(shell, 'tool_window_manage', manage)
    args = {'action': action, 'target': 123, 'x': 50, 'y': 60,
            'width': 700, 'height': 500, 'index': 1, 'above': True}
    assert shell.tool_window_layout(args) == result
    assert captured == [args]


def test_layout_remains_state_changing_and_halt_guarded(monkeypatch):
    assert 'window_layout' not in server._READ_ONLY_TOOLS
    monkeypatch.setattr(shell, 'halt_active', lambda: True)
    sent = []
    token = CURRENT.set(Execution(clock()+10))
    try:
        with pytest.raises(ToolError) as error:
            execute('window_layout', {'action': 'minimize', 'target': 123},
                    handler=lambda args: sent.append(args))
    finally:
        CURRENT.reset(token)
    assert error.value.code == 'halted'
    assert sent == []


def test_registry_limits_layout_and_leaves_general_actions_unexempted():
    tools = {tool['name']: tool for tool in server.TOOLS}
    allowed = set(tools['window_layout']['inputSchema']['properties']['action']['enum'])
    assert allowed == {'move_resize', 'minimize', 'unminimize', 'maximize',
                       'unmaximize', 'workspace', 'above'}
    assert 'close' in tools['window_manage']['inputSchema']['properties']['action']['enum']
    for name in ('activate_window', 'window_layout'):
        assert tools[name]['annotations']['readOnlyHint'] is False
        assert tools[name]['annotations']['destructiveHint'] is False
        assert tools[name]['annotations']['openWorldHint'] is False
    for name in ('window_manage', 'launch_app', 'pointer_click', 'pointer_path',
                 'type_text', 'press_keys', 'do_steps'):
        assert not tools[name].get('annotations', {}).get('readOnlyHint', False)
