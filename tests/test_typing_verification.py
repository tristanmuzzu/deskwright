"""Typing regressions from Files and Calculator; no live input."""
from types import SimpleNamespace as NS

import pytest

from deskwright import input as wi
from deskwright.errors import ToolError


@pytest.fixture
def typing(monkeypatch):
    class Text:
        value = '/tmp/trial'
        selection = (0, len(value))
        caret = len(value)
        outcome = '/tmp/trial/Reviewed'
        after_selection = None
        calls = 0

        def clear_cache_single(self):
            pass

        def get_text_iface(self):
            return self

        def get_n_selections(self):
            return int(self.selection is not None)

        def get_selection(self, _):
            return NS(start_offset=self.selection[0], end_offset=self.selection[1])

        def get_caret_offset(self):
            return self.caret

        def type_text(self, text, delay):
            self.calls += 1
            self.value = self.outcome
            self.selection = self.after_selection
            self.caret = len(self.value)

    node = Text()
    ticks, sleeps = [0.0], []

    def pause(seconds):
        sleeps.append(seconds)
        ticks[0] += seconds

    monkeypatch.setattr(wi, 'clock', lambda: ticks[0], raising=False)
    monkeypatch.setattr(wi, 'pause', pause)
    monkeypatch.setattr(wi, 'focus_window', lambda _: {
        'window': {'wm_class': 'editor', 'title': 'Test'}, 'detail': 'focused'})
    monkeypatch.setattr(wi, '_look_before', lambda *a, **k: None)
    monkeypatch.setattr(wi, '_look_typed', lambda a, r, *args: r)
    monkeypatch.setattr(wi, '_atspi_app_for_window', lambda _: 'editor')
    monkeypatch.setattr(wi, 'ensure_widget_focus', lambda *a, **kw: {
        'state': 'already', 'path': 'editor/0'})
    monkeypatch.setattr(wi, '_find_text_widget', lambda *a: node)
    monkeypatch.setattr(wi, '_read_text', lambda n: n.value)
    monkeypatch.setattr(wi, '_pointer', lambda: node)
    monkeypatch.setattr(wi, '_atspi', lambda: NS(Text=NS(
        get_n_selections=lambda n: n.get_n_selections(),
        get_selection=lambda n, i: n.get_selection(i),
        get_caret_offset=lambda n: n.get_caret_offset())), raising=False)
    return NS(node=node, sleeps=sleeps)


def test_selected_path_replacement_sharing_prefix_has_no_fixed_wait(typing):
    out = wi.tool_type_text({'target': 1, 'text': '/tmp/trial/Reviewed'})
    assert out['verified'] and typing.node.calls == 1
    assert typing.sleeps == []


def test_selected_autocomplete_suffix_does_not_cause_retry(typing):
    typing.node.outcome += '/'
    typing.node.after_selection = (len('/tmp/trial/Reviewed'), len(typing.node.outcome))
    assert wi.tool_type_text({'target': 1, 'text': '/tmp/trial/Reviewed'})['verified']


def test_unselected_extra_text_is_not_success(typing):
    typing.node.outcome += '/wrong'
    with pytest.raises(ToolError, match='readback did not confirm'):
        wi.tool_type_text({'target': 1, 'text': '/tmp/trial/Reviewed'})
    assert typing.node.calls == 1


def test_calculator_normalization_requires_explicit_full_postcondition(typing):
    typing.node.value = ''
    typing.node.selection = (0, 0)
    typing.node.outcome = '3\u00d74'
    with pytest.raises(ToolError):
        wi.tool_type_text({'target': 1, 'text': '3*4'})
    typing.node.value = ''
    assert wi.tool_type_text({'target': 1, 'text': '3*4', 'expected_after': '3\u00d74'})['verified']


def test_unchanged_preexisting_text_is_not_mistaken_for_insertion(typing):
    typing.node.value = typing.node.outcome = 'hello'
    typing.node.selection = None
    typing.node.caret = 5
    with pytest.raises(ToolError):
        wi.tool_type_text({'target': 1, 'text': 'hello'})
    assert typing.node.calls == 1


def test_middle_selection_preserves_both_sides(typing):
    typing.node.value = 'head OLD tail'
    typing.node.selection = (5, 8)
    typing.node.outcome = 'head NEW tail'
    assert wi.tool_type_text({'target': 1, 'text': 'NEW'})['verified']


def test_readback_keeps_native_widget_when_child_path_retargets(typing, monkeypatch):
    calls = []

    def find(*_):
        calls.append(1)
        return typing.node if len(calls) == 1 else NS(value='unrelated')

    monkeypatch.setattr(wi, '_find_text_widget', find)
    assert wi.tool_type_text({'target': 1, 'text': '/tmp/trial/Reviewed'})['verified']
    assert len(calls) == 1


def test_known_normalization_is_exact_not_a_substring(typing):
    typing.node.outcome = '3\u00d74wrong'
    typing.node.after_selection = (3, len(typing.node.outcome))
    with pytest.raises(ToolError):
        wi.tool_type_text({'target': 1, 'text': '3*4', 'expected_after': '3\u00d74'})


@pytest.mark.parametrize('delay', [-1, True, 1.5, 1001])
def test_invalid_delay_refuses_before_focus(typing, monkeypatch, delay):
    monkeypatch.setattr(wi, 'focus_window', lambda _: pytest.fail('focused'))
    with pytest.raises(ToolError) as caught:
        wi.tool_type_text({'target': 1, 'text': 'x', 'key_delay_ms': delay})
    assert caught.value.code == 'bad_args'


def test_default_keysym_delay_is_eight_ms(typing):
    original = typing.node.type_text
    seen = []

    def type_text(text, delay):
        seen.append(delay)
        original(text, delay)

    typing.node.type_text = type_text
    wi.tool_type_text({'target': 1, 'text': '/tmp/trial/Reviewed'})
    assert seen == [.008]
