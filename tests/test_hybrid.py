"""Generic observation routing and recovery; no live desktop needed."""
from types import SimpleNamespace as NS

import pytest

from deskwright import capture, code_runtime, hybrid, semantic, server, shell
from deskwright.errors import ToolError


@pytest.fixture
def route(monkeypatch):
    hybrid.ROUTES.clear()
    win = {'id': 42, 'pid': 10, 'title': 'Arbitrary app', 'wm_class': 'fixture',
           'x': 0, 'y': 0, 'width': 500, 'height': 400, 'focused': True}
    now = [10.0]
    calls = []
    rows = [{'ref': 'test', 'role': 'button', 'name': 'Proceed',
             'states': ['visible', 'showing', 'sensitive'], 'actions': [{'index': 0, 'name': 'click'}]}]
    state = {'complete': True, 'controls': rows, 'window': win,
             'visited': 30, 'limits_hit': [], 'content_trust': 'untrusted'}
    monkeypatch.setattr(shell, 'list_windows', lambda: [win])
    monkeypatch.setattr(semantic, '_start', lambda _: 'process-one')
    monkeypatch.setattr(hybrid, 'clock', lambda: now[0])
    def scan(args):
        calls.append(('native', args))
        return state
    def shot(args):
        calls.append(('visual', args))
        return {'observation': {'id': 'frame'}, capture._INLINE_KEY: {'data': 'pixels'}}
    monkeypatch.setattr(semantic, 'tool_ui_snapshot', scan)
    monkeypatch.setattr(capture, 'tool_screenshot', shot)
    return NS(win=win, now=now, calls=calls, state=state)


def test_fast_native_observation_avoids_capture(route):
    out = hybrid.tool_ui_observe({'window_id': 42})
    assert out['mode'] == 'native' and out['complete']
    assert [c[0] for c in route.calls] == ['native']
    assert route.calls[0][1]['budget_ms'] == 1000


def test_incomplete_scan_falls_back_same_response_and_cools_down(route):
    route.state.update(complete=False, limits_hit=['time_budget'])
    first = hybrid.tool_ui_observe({'window_id': 42})
    second = hybrid.tool_ui_observe({'window_id': 42})
    assert first['mode'] == second['mode'] == 'visual'
    assert first['controls'] == []  # partial targeting is never promoted to success
    assert first[capture._INLINE_KEY]['data'] == 'pixels'
    assert [c[0] for c in route.calls] == ['native', 'visual', 'visual']
    assert second['reason'].startswith('native_probe_cooldown')


@pytest.mark.parametrize('change', ['title', 'pid', 'geometry', 'expiry', 'session', 'force'])
def test_route_cache_never_reuses_targets_and_can_recover(route, monkeypatch, change):
    route.state.update(complete=False, limits_hit=['node_limit'])
    hybrid.tool_ui_observe({'window_id': 42})
    route.state.update(complete=True, limits_hit=[])
    args = {'window_id': 42}
    if change == 'title':
        route.win['title'] = 'Next document'
    elif change == 'pid':
        route.win['pid'] = 11
    elif change == 'geometry':
        route.win['width'] += 1
    elif change == 'expiry':
        route.now[0] += 31
    elif change == 'session':
        monkeypatch.setattr(hybrid, 'session_key', lambda: 'other')
    else:
        args['mode'] = 'native'
    assert hybrid.tool_ui_observe(args)['mode'] == 'native'
    assert route.calls[-1][0] == 'native'


def test_canvas_or_explicit_visual_never_requires_semantic_actions(route):
    route.state['controls'] = [{'role': 'frame', 'states': [], 'name': 'Canvas', 'ref': 'x'}]
    assert hybrid.tool_ui_observe({'window_id': 42})['reason'] == 'no_actionable_native_controls'
    route.calls.clear()
    assert hybrid.tool_ui_observe({'window_id': 42, 'mode': 'visual'})['mode'] == 'visual'
    assert [c[0] for c in route.calls] == ['visual']


def test_drawing_surface_keeps_pixels_even_with_native_toolbar(route):
    route.state['visual_content'] = True
    out = hybrid.tool_ui_observe({'window_id': 42})
    assert out['mode'] == 'visual' and out['reason'] == 'visual_content'
    assert out['controls'][0]['name'] == 'Proceed'
    assert capture._INLINE_KEY in out


@pytest.mark.parametrize('mode', ['auto', 'native', 'visual'])
def test_unmapped_window_skips_accessibility_startup(route, mode):
    route.win['width'] = 0
    out = hybrid.tool_ui_observe({'window_id': 42, 'mode': mode})
    assert out['reason'] == 'window_still_mapping'
    assert [c[0] for c in route.calls] == ['visual']
    assert 'window' not in route.calls[0][1]  # no zero-sized crop


def test_read_only_recovery_failure_never_hides_original_error(monkeypatch):
    def fail(_):
        raise ToolError('specific ambiguous target', code='ambiguous_control', action_status='not_started')
    monkeypatch.setitem(server.HANDLERS, 'ui_query', fail)
    monkeypatch.setattr(code_runtime.Desktop, 'recovery', lambda _: (_ for _ in ()).throw(RuntimeError('capture lost')))
    out = code_runtime.tool_desktop_exec({'reset': True, 'code':
        "desktop.query(42, field={'role':'text'})"})
    assert out['error']['code'] == 'ambiguous_control'
    assert out['recovery_observation']['observation_unavailable'] == 'RuntimeError'


def test_process_replacement_during_capture_invalidates_observation(route, monkeypatch):
    calls = iter(['original-process', 'replacement-process'])
    monkeypatch.setattr(semantic, '_start', lambda _: next(calls))
    with pytest.raises(ToolError, match='process changed'):
        hybrid.tool_ui_observe({'window_id': 42, 'mode': 'visual'})


def test_shared_argument_check_refuses_unknown_fields_and_choices():
    with pytest.raises(ToolError, match='unknown arguments'):
        server.validate_tool_args('pointer_click', {'x': 1, 'y': 2, 'clicks': 2})
    with pytest.raises(ToolError, match='invalid choices'):
        server.validate_tool_args('ui_observe', {'window_id': 1, 'mode': 'guess'})


def test_unavailable_accessibility_uses_pixels_but_halt_is_not_swallowed(route, monkeypatch):
    def missing(args):
        raise ToolError('provider absent', code='app_not_on_bus')
    monkeypatch.setattr(semantic, 'tool_ui_snapshot', missing)
    assert hybrid.tool_ui_observe({'window_id': 42})['mode'] == 'visual'
    def halted(args):
        raise ToolError('human halt', code='halted')
    monkeypatch.setattr(semantic, 'tool_ui_snapshot', halted)
    with pytest.raises(ToolError, match='human halt'):
        hybrid.tool_ui_observe({'window_id': 42, 'mode': 'native'})


def test_observe_helper_displays_fallback_once_without_base64_text(monkeypatch):
    monkeypatch.setitem(server.HANDLERS, 'ui_observe', lambda _: {
        'mode': 'visual', 'controls': [], capture._INLINE_KEY: {'data': 'fixture-pixels'}})
    out = code_runtime.tool_desktop_exec({'reset': True, 'code': 'log(desktop.observe(42))'})
    assert out['all_ok']
    assert len(out[capture._INLINE_KEY+'s']) == 1
    assert 'fixture-pixels' not in str(out['output'])


def test_unknown_click_argument_is_refused_before_input(monkeypatch):
    monkeypatch.setitem(server.HANDLERS, 'pointer_click', lambda _: pytest.fail('sent input'))
    out = code_runtime.tool_desktop_exec({'reset': True, 'code':
        'desktop.click(10, 20, target=42, clicks=2)'})
    assert out['error']['code'] == 'bad_args'
    assert out['action_status'] == 'not_started'
    assert 'count' in out['error']['details']['schema']['properties']


def test_failed_postcondition_attaches_pixels_and_stops_input(monkeypatch):
    calls = []
    monkeypatch.setattr(shell, 'halt_active', lambda: False)
    monkeypatch.setattr(shell, 'list_windows', lambda: [
        {'id': 42, 'pid': 10, 'title': 'New modal', 'wm_class': 'fixture', 'focused': True}])
    monkeypatch.setitem(server.HANDLERS, 'press_keys', lambda _: calls.append('input') or {})
    def query(_):
        raise ToolError('pending', code='timeout', action_status='not_started',
                        details={'last_complete_observation': 'missing result'})
    monkeypatch.setitem(server.HANDLERS, 'ui_query', query)
    monkeypatch.setitem(server.HANDLERS, 'screenshot', lambda _: {
        capture._INLINE_KEY: {'data': 'fresh'}, 'observation': {'id': 'fresh-id'}})
    out = code_runtime.tool_desktop_exec({'reset': True, 'code':
        "desktop.key('Enter', target=42)\n"
        "desktop.query(42, result={'role':'text','text':'done'})\n"
        "desktop.key('Enter', target=42)"})
    assert calls == ['input']
    assert out['action_status'] == 'partial'
    assert out['error']['action_status'] == 'not_started'
    assert out['error']['details']['last_complete_observation'] == 'missing result'
    assert out['recovery_observation']['input_replayed'] is False
    assert len(out[capture._INLINE_KEY+'s']) == 1


def test_failed_required_wait_preserves_actual_window_evidence(monkeypatch):
    monkeypatch.setattr(code_runtime.Desktop, 'recovery', lambda _: {'fixture': True})
    monkeypatch.setitem(server.HANDLERS, 'wait_for', lambda _: {
        'met': False, 'windows': [{'id': 42, 'wm_class': 'actual-class'}]})
    out = code_runtime.tool_desktop_exec({'reset': True, 'code':
        "desktop.wait('window_exists', target='guessed-class', timeout=.2)"})
    assert out['error']['details']['windows'][0]['wm_class'] == 'actual-class'


def test_halted_batch_never_attempts_recovery(monkeypatch):
    monkeypatch.setattr(code_runtime.Desktop, 'recovery', lambda _: pytest.fail('recovery after halt'))
    def halted(_):
        raise ToolError('stop', code='halted')
    monkeypatch.setitem(server.HANDLERS, 'press_keys', halted)
    out = code_runtime.tool_desktop_exec({'reset': True, 'code': "desktop.key('Enter', target=42)"})
    assert out['error']['code'] == 'halted'


def test_new_window_discovery_does_not_guess_classes_or_pick_one(monkeypatch):
    windows = [{'id': i, 'wm_class': 'unfamiliar', 'title': f'Window{i}',
                'transient_for': 1} for i in range(1, 4)]
    monkeypatch.setattr(shell, 'list_windows', lambda: windows)
    result = shell.tool_wait_for({'condition': 'window_new', 'since': [1], 'timeout': .2})
    assert result['met'] and [w['id'] for w in result['matched']] == [2, 3]
    assert result['matched_count'] == 2


def test_exact_title_wait_uses_target_identity(monkeypatch):
    windows = [{'id': 1, 'wm_class': 'files', 'title': 'Old'},
               {'id': 2, 'wm_class': 'files', 'title': 'Destination'}]
    monkeypatch.setattr(shell, 'list_windows', lambda: windows)
    assert not shell.tool_wait_for({'condition': 'window_title', 'target': 1,
                                   'title': 'Destination', 'timeout': .2})['met']
    windows[0]['title'] = 'Destination'
    assert shell.tool_wait_for({'condition': 'window_title', 'target': 1,
                               'title': 'Destination', 'timeout': .2})['met']


@pytest.mark.parametrize('arguments', [[], '', 0, False, None])
def test_mcp_refuses_non_object_arguments_before_starting_desktop(monkeypatch, arguments):
    from deskwright import session
    replies = []
    monkeypatch.setattr(session, 'start_deferred', lambda: pytest.fail('started desktop for invalid arguments'))
    monkeypatch.setattr(server, '_respond', lambda ident, result: replies.append(result))
    server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'list_windows', 'arguments': arguments}})
    assert replies[0]['isError']
    assert 'bad_args' in str(replies[0])


def test_disappeared_window_returns_recovery_without_input(monkeypatch):
    def missing(_):
        raise ToolError('window gone', code='window_not_found')
    monkeypatch.setitem(server.HANDLERS, 'ui_observe', missing)
    monkeypatch.setattr(code_runtime.Desktop, 'recovery', lambda _: {'current_window': 43})
    result = code_runtime.tool_desktop_exec({'reset': True, 'code': 'desktop.observe(42)'})
    assert result['action_status'] == 'not_started'
    assert result['recovery_observation']['current_window'] == 43


def test_compact_observation_keeps_parent_identity_for_unnamed_fields(route):
    route.state['controls'][0]['parent'] = 'observed-panel-ref'
    result = hybrid.tool_ui_observe({'window_id': 42})
    assert result['controls'][0]['parent'] == 'observed-panel-ref'
