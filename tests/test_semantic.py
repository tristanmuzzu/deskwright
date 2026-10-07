"""Semantic contract and failure paths; fake providers, no desktop input."""
from types import SimpleNamespace as NS

import pytest

from deskwright import atspi
from deskwright import semantic as ui
from deskwright.errors import ToolError


class Node:
    def __init__(self, name, role="button", children=()):
        self.name, self.role = name, role
        self.children = list(children)
        self.parent = None
        self.app = NS(bus_name=":1.42")
        self.path = f"/a/{id(self)}"
        self.states = {"sensitive", "showing", "visible"}
        self.actions = ["click"] if role in ("button", "check box") else []
        self.value = ""
        self.presses = 0
        if role == "text":
            self.states.add("editable")
        for child in self.children:
            child.parent = self

    def clear_cache_single(self):
        pass

    def get_state_set(self):
        return NS(get_states=lambda: [NS(value_nick=s) for s in self.states])

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_parent(self):
        return self.parent

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, i):
        return self.actions[i]

    def get_editable_text_iface(self):
        return self if "editable" in self.states else None

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def do_action(self, i):
        self.presses += 1
        if self.role == "check box":
            self.states.symmetric_difference_update({"checked"})
        return True

    def insert_text(self, offset, text, length):
        text = text.encode()[:length].decode()
        self.value = self.value[:offset] + text + self.value[offset:]
        return True


@pytest.fixture
def env(monkeypatch):
    ui.REFS.clear()
    button, text, toggle = Node("Save"), Node("Title", "text"), Node("Ready", "check box")
    root = Node("Document", "frame", [button, text, toggle])
    app = Node("App", "application", [root])
    win = {"id": 42, "pid": 10, "title": "Document", "minimized": False}
    state = {"locked": False, "modal_count": 0, "overview": False,
             "focused_window": 42, "windows": [win]}
    monkeypatch.setattr(ui.shell, "list_windows", lambda: state["windows"])
    monkeypatch.setattr(ui.shell, "interaction_state", lambda: state)
    monkeypatch.setattr(ui.shell, "halt_active", lambda: False)
    monkeypatch.setattr(atspi, "_find_app", lambda _: app)
    monkeypatch.setattr(ui, "_start", lambda _: "123")
    monkeypatch.setattr(atspi, "_extents_problem", lambda _: None)
    monkeypatch.setattr(atspi, "_text_ifaces", lambda n: (n, n.get_editable_text_iface()))
    monkeypatch.setattr(atspi, "_read_text", lambda n: n.value)

    def delete(n, start, end):
        n.value = n.value[:start] + n.value[end:]
        return True

    api = NS(Text=NS(get_character_count=lambda n: len(n.value),
                     get_text=lambda n, start, end: n.value[start:end]),
             EditableText=NS(delete_text=delete))
    monkeypatch.setattr(atspi, "_atspi", lambda: api)
    snapshot = ui.tool_ui_snapshot({"window_id": 42})
    refs = {n["name"]: n["ref"] for n in snapshot["controls"]}
    return NS(button=button, text=text, toggle=toggle, root=root, app=app,
              win=win, state=state, refs=refs, api=api, snapshot=snapshot)


def error(call, args, code):
    with pytest.raises(ToolError) as caught:
        call(args)
    assert caught.value.code == code
    return caught.value


def test_snapshot_scoped_complete_and_limits(env):
    assert env.snapshot["complete"]
    assert env.snapshot["visited"] == 4
    assert len(env.snapshot["controls"]) == 4
    bounded = ui.tool_ui_snapshot({"window_id": 42, "limit": 1})
    assert not bounded["complete"] and "result_limit" in bounded["limits_hit"]
    scoped = ui.tool_ui_snapshot({"window_id": 42, "role": "button", "name": "sav"})
    assert [n["name"] for n in scoped["controls"]] == ["Save"]
    assert scoped["complete"]


def test_query_reads_multiple_exact_controls_in_one_scan(env):
    env.text.value = "draft"
    out = ui.tool_ui_query({"window_id": 42, "controls": {
        "field": {"role": "text", "include_text": True},
        "save": {"role": "button", "name": "Save", "ready": True},
    }})
    assert out["met"] and out["attempts"] == 1
    assert out["controls"]["field"]["text"] == "draft"
    ref = out["controls"]["save"]["ref"]
    assert ui.tool_ui_action({"ref": ref, "action": "invoke"})["action_status"] == "accepted"


def test_filtered_snapshot_skips_irrelevant_action_tables(env, monkeypatch):
    def unexpected():
        raise AssertionError("unrelated action table was read")
    monkeypatch.setattr(env.button, "get_action_iface", unexpected)
    out = ui.tool_ui_snapshot({"window_id": 42, "role": "text"})
    assert out["complete"] and len(out["controls"]) == 1


def test_query_never_picks_first_or_ready_duplicate(env):
    duplicate = Node("Save")
    duplicate.states.remove("sensitive")
    duplicate.parent = env.root
    env.root.children.append(duplicate)
    error(ui.tool_ui_query, {"window_id": 42, "controls": {
        "save": {"role": "button", "name": "Save", "ready": True}}}, "ambiguous_control")
    assert env.button.presses == duplicate.presses == 0


def test_query_exact_names_ignore_hidden_duplicates(env):
    duplicate = Node("Save")
    duplicate.states.remove("showing")
    duplicate.parent = env.root
    env.root.children.append(duplicate)
    out = ui.tool_ui_query({"window_id": 42, "controls": {
        "save": {"role": "button", "name": "Save"}}})
    assert ui.REFS[out["controls"]["save"]["ref"]].node is env.button
    error(ui.tool_ui_query, {"window_id": 42, "timeout_s": 0, "controls": {
        "save": {"role": "button", "name": "sav"}}}, "timeout")


def test_query_absence_requires_complete_tree(env, monkeypatch):
    args = {"window_id": 42, "timeout_s": 0,
            "controls": {"dialog": {"role": "dialog", "absent": True}}}
    assert ui.tool_ui_query(args)["controls"]["dialog"] is None
    monkeypatch.setattr(env.button, "get_name", lambda: 1/0)
    error(ui.tool_ui_query, args, "timeout")


def test_query_single_match_requires_complete_tree(env, monkeypatch):
    monkeypatch.setattr(env.toggle, "get_name", lambda: 1/0)
    error(ui.tool_ui_query, {"window_id": 42, "timeout_s": 0, "controls": {
        "save": {"role": "button", "name": "Save"}}}, "timeout")


def test_query_waits_for_startup_ready_and_exact_text(env, monkeypatch):
    tick = [0.0]
    monkeypatch.setattr(ui, "clock", lambda: tick[0])
    env.text.value = "loading"
    env.button.states.remove("sensitive")
    def advance(seconds):
        tick[0] += seconds
        env.text.value = "done"
        env.button.states.add("sensitive")
    monkeypatch.setattr(ui, "pause", advance)
    def app(_):
        if not tick[0]:
            raise ToolError("starting", code="app_not_on_bus")
        return env.app
    monkeypatch.setattr(atspi, "_find_app", app)
    out = ui.tool_ui_query({"window_id": 42, "controls": {
        "save": {"role": "button", "ready": True},
        "field": {"role": "text", "text": "done"}}})
    assert out["attempts"] == 2 and out["controls"]["field"]["text"] == "done"
    assert env.button.presses == 0


def test_query_waits_for_disappearance_without_replaying_action(env, monkeypatch):
    tick = [0.0]
    monkeypatch.setattr(ui, "clock", lambda: tick[0])
    def advance(seconds):
        tick[0] += seconds
        env.button.states.remove("showing")
    monkeypatch.setattr(ui, "pause", advance)
    out = ui.tool_ui_query({"window_id": 42, "controls": {
        "save": {"role": "button", "absent": True}}})
    assert out["attempts"] == 2 and env.button.presses == 0


def test_query_does_not_weaken_action_focus_guard(env):
    ref = ui.tool_ui_query({"window_id": 42, "controls": {
        "save": {"role": "button", "ready": True}}})["controls"]["save"]["ref"]
    env.state["focused_window"] = 99
    error(ui.tool_ui_action, {"ref": ref, "action": "invoke"}, "focus_not_acquired")
    assert env.button.presses == 0


def test_query_does_not_restart_slow_complete_scan_at_snapshot_default(env, monkeypatch):
    budgets = []
    original = ui._snapshot
    def observed(args, **kwargs):
        budgets.append(args['budget_ms'])
        return original(args, **kwargs)
    monkeypatch.setattr(ui, '_snapshot', observed)
    out = ui.tool_ui_query({'window_id': 42, 'controls': {'save': {'role': 'button'}}})
    assert out['attempts'] == 1 and 4500 < budgets[0] <= 5000


def test_scoped_query_avoids_unrelated_tree_and_keeps_window_guards(env):
    inside = Node('Save')
    panel = Node('Options', 'panel', [inside])
    panel.parent = env.root
    env.root.children.append(panel)
    ref = ui.tool_ui_snapshot({'window_id': 42, 'role': 'panel'})['controls'][0]['ref']
    error(ui.tool_ui_query, {'window_id': 42, 'controls': {'save': {'role': 'button', 'name': 'Save'}}},
          'ambiguous_control')
    # The unrelated sibling becomes unreadable; a scoped scan must not visit it.
    env.button.get_state_set = lambda: (_ for _ in ()).throw(RuntimeError('unrelated provider'))
    out = ui.tool_ui_query({'window_id': 42, 'within': ref,
                           'controls': {'save': {'role': 'button', 'name': 'Save'}}})
    assert out['scope'] == ref
    env.state['focused_window'] = 99
    error(ui.tool_ui_action, {'ref': out['controls']['save']['ref'], 'action': 'invoke'},
          'focus_not_acquired')
    assert inside.presses == 0


def test_scope_cannot_cross_window_or_survive_detachment(env):
    ref = env.refs['Title']
    ui.REFS[ref].window['id'] = 99
    error(ui.tool_ui_snapshot, {'window_id': 42, 'within': ref}, 'stale_observation')
    ui.REFS[ref].window['id'] = 42
    env.text.parent = None
    error(ui.tool_ui_snapshot, {'window_id': 42, 'within': ref}, 'stale_observation')


def test_query_timeout_retains_last_complete_reason(env, monkeypatch):
    tick = [0.0]
    monkeypatch.setattr(ui, 'clock', lambda: tick[0])
    monkeypatch.setattr(ui, 'pause', lambda seconds: None)
    def snapshots(args, **kwargs):
        tick[0] += 0.6
        complete = tick[0] < 1
        return {'window': env.win, 'controls': [], 'complete': complete,
                'limits_hit': [] if complete else ['time_budget']}
    monkeypatch.setattr(ui, '_snapshot', snapshots)
    with pytest.raises(ToolError) as exc:
        ui.tool_ui_query({'window_id': 42, 'timeout_s': 1,
                          'controls': {'renamed': {'role': 'button', 'name': 'Old Label'}}})
    assert 'conditions not met: renamed' in str(exc.value)
    assert exc.value.details['scan_limits'] == ['time_budget']


def test_query_refuses_process_change_during_wait(env, monkeypatch):
    def change(_):
        env.win["pid"] = 11
    monkeypatch.setattr(ui, "pause", change)
    error(ui.tool_ui_query, {"window_id": 42, "controls": {
        "dialog": {"role": "dialog"}}}, "stale_observation")


@pytest.mark.parametrize("selector", [
    {}, {"role": 5}, {"role": "button", "ready": 1},
    {"role": "button", "name": None}, {"role": "button", "unexpected": True},
    {"role": "button", "absent": True, "text": "done"},
])
def test_query_validates_before_scanning(env, selector, monkeypatch):
    monkeypatch.setattr(ui.shell, "list_windows", lambda: pytest.fail("scan before validation"))
    error(ui.tool_ui_query, {"window_id": 42, "controls": {"x": selector}}, "bad_args")


def test_duplicate_window_titles_are_refused(env):
    env.app.children.append(Node("Document", "frame"))
    error(ui.tool_ui_snapshot, {"window_id": 42}, "widget_missing")


@pytest.mark.parametrize("change", ["name", "role", "actions", "defunct", "detach", "native_path"])
def test_ref_cannot_retarget_changed_object(env, change):
    if change == "name":
        env.button.name = "Save elsewhere"
    elif change == "role":
        env.button.role = "link"
    elif change == "actions":
        env.button.actions.append("delete")
    elif change == "defunct":
        env.button.states.add("defunct")
    elif change == "detach":
        env.button.parent = None
    else:
        env.button.path += "/new"
    out = error(ui.tool_ui_action, {"ref": env.refs["Save"], "action": "invoke"}, "stale_observation")
    assert out.action_status == "not_started"
    assert env.button.presses == 0


def test_ref_survives_sibling_reordering_without_retargeting(env):
    env.root.children.reverse()
    out = ui.tool_ui_action({"ref": env.refs["Save"], "action": "invoke"})
    assert out["action_status"] == "accepted" and not out["verified"]
    assert env.button.presses == 1 and env.toggle.presses == 0


@pytest.mark.parametrize("change", ["expired", "session", "process", "reset"])
def test_ref_lifetime(env, monkeypatch, change):
    ref = env.refs["Save"]
    if change == "expired":
        ui.REFS[ref].expires = 0
    elif change == "session":
        monkeypatch.setattr(ui, "session_key", lambda: "different")
    elif change == "process":
        monkeypatch.setattr(ui, "_start", lambda _: "456")
    else:
        ui.REFS.clear()
    error(ui.tool_ui_action, {"ref": ref, "action": "invoke"}, "stale_observation")
    assert env.button.presses == 0


@pytest.mark.parametrize(("field", "value", "code"), [
    ("locked", True, "occluded"), ("modal_count", 1, "occluded"),
    ("overview", True, "occluded"), ("focused_window", 99, "focus_not_acquired"),
])
def test_shell_blockers_refuse_before_dispatch(env, field, value, code):
    env.state[field] = value
    error(ui.tool_ui_action, {"ref": env.refs["Save"], "action": "invoke"}, code)
    assert env.button.presses == 0


def test_modal_child_blocks_even_if_parent_claims_focus(env):
    env.state["windows"].append({"id": 43, "transient_for": 42, "modal": True})
    error(ui.tool_ui_action, {"ref": env.refs["Save"], "action": "invoke"}, "occluded")
    assert env.button.presses == 0


@pytest.mark.parametrize("state", ["sensitive", "showing", "visible"])
def test_hidden_disabled_widget_is_not_invoked(env, state):
    env.button.states.remove(state)
    error(ui.tool_ui_action, {"ref": env.refs["Save"], "action": "invoke"}, "widget_missing")
    assert env.button.presses == 0


def test_halt_is_not_bypassed(env, monkeypatch):
    monkeypatch.setattr(ui.shell, "halt_active", lambda: True)
    error(ui.tool_ui_action, {"ref": env.refs["Save"], "action": "invoke"}, "halted")
    assert env.button.presses == 0


def test_checkbox_assignment_is_idempotent(env):
    args = {"ref": env.refs["Ready"], "action": "set_checked", "checked": True}
    assert ui.tool_ui_action(args)["verified"]
    assert ui.tool_ui_action(args)["changed"] is False
    assert env.toggle.presses == 1


def test_text_compare_and_exact_unicode_write(env):
    env.text.value = "original\n"
    args = {"ref": env.refs["Title"], "action": "set_text", "text": "  Café 山 🏔️  ",
            "expected_text": "original\n"}
    assert ui.tool_ui_action(args)["verified"]
    assert env.text.value == args["text"]
    error(ui.tool_ui_action, args, "stale_observation")
    read = ui.tool_ui_inspect({"ref": env.refs["Title"], "include_text": True, "max_characters": 3})
    assert read["text_truncated"] and read["text"] == "  C"


def test_failed_delete_never_inserts(env):
    env.text.value = "keep"
    env.api.EditableText.delete_text = lambda *args: False
    error(ui.tool_ui_action, {"ref": env.refs["Title"], "action": "set_text",
                             "text": "new", "expected_text": "keep"}, "atspi_write_failed")
    assert env.text.value == "keep"


def test_false_success_and_whitespace_mismatch_are_not_verified(env):
    env.text.insert_text = lambda *args: True
    error(ui.tool_ui_action, {"ref": env.refs["Title"], "action": "set_text",
                             "text": " ", "expected_text": "", "timeout_s": 0}, "atspi_write_failed")


def test_wait_times_out_without_replaying_action(env):
    error(ui.tool_ui_wait, {"ref": env.refs["Ready"], "checked": True, "timeout_s": 0}, "timeout")
    assert env.toggle.presses == 0


@pytest.mark.parametrize("args", [{"limit": 0}, {"max_nodes": True}, {"depth": -1},
                                  {"budget_ms": float("nan")}, {"name": []}])
def test_bad_snapshot_arguments(env, args):
    error(ui.tool_ui_snapshot, {"window_id": 42, **args}, "bad_args")


def test_missing_extension_guard_is_refused(monkeypatch):
    monkeypatch.setattr(ui.shell, "extension_methods", lambda: set())
    error(lambda _: ui.shell.interaction_state(), {}, "needs_relogin")


def test_many_actions_do_not_hide_descendant_controls(env):
    env.root.actions = [f"window.action{i}" for i in range(100)]
    snapshot = ui.tool_ui_snapshot({"window_id": 42})
    assert snapshot["complete"]
    frame = snapshot["controls"][0]
    assert frame["actions_truncated"] and frame["action_count"] == 100
    assert len(frame["actions"]) == 8
    assert any(n["name"] == "Title" for n in snapshot["controls"])
    error(ui.tool_ui_action, {"ref": frame["ref"], "action": "invoke", "action_index": 8}, "bad_args")


def test_large_child_list_does_not_overrun_node_budget(env):
    env.root.children = [Node(str(i)) for i in range(1000)]
    snapshot = ui.tool_ui_snapshot({"window_id": 42, "max_nodes": 5})
    assert snapshot["visited"] <= 5
    assert not snapshot["complete"] and "node_limit" in snapshot["limits_hit"]


def test_reference_pool_evicts_oldest_without_reusing_tokens(env, monkeypatch):
    monkeypatch.setattr(ui, "MAX_REFS", 4)
    old = env.refs["Save"]
    fresh = ui.tool_ui_snapshot({"window_id": 42})
    assert len(ui.REFS) == 4
    assert all(n["ref"] != old for n in fresh["controls"])
    error(ui.tool_ui_inspect, {"ref": old}, "stale_observation")


def test_action_completion_polls_without_repeating_write(env, monkeypatch):
    ticks = [0.0]
    reads = iter(["", "", "new"])
    monkeypatch.setattr(atspi, "clock", lambda: ticks[0])
    monkeypatch.setattr(atspi, "pause", lambda delay: ticks.__setitem__(0, ticks[0]+delay))
    monkeypatch.setattr(atspi, "_read_text", lambda _: next(reads))
    calls = []
    env.text.insert_text = lambda *args: calls.append(args) or True
    assert atspi._write_text(env.text, "new", replace=True) == ("", "new")
    assert calls == [(0, "new", 3)]
    assert ticks[0] == .02


def test_immediate_completion_has_no_fixed_sleep(env, monkeypatch):
    monkeypatch.setattr(atspi, "pause", lambda _: pytest.fail("unnecessary sleep"))
    assert atspi._write_text(env.text, "new", replace=True) == ("", "new")


def test_missing_shell_fields_fail_closed(monkeypatch):
    monkeypatch.setattr(ui.shell, "extension_methods", lambda: {"InteractionState"})
    monkeypatch.setattr(ui.shell, "_gdbus", lambda *a: "('{}',)")
    error(lambda _: ui.shell.interaction_state(), {}, "extension_unavailable")


def test_gtk4_dynamic_undo_actions_do_not_invalidate_text_reads(env):
    ref = env.refs["Title"]
    env.text.actions = ["text.undo", "text.redo"]
    env.text.value = "edited"
    assert ui.tool_ui_inspect({"ref": ref, "include_text": True})["text"] == "edited"
    assert ui.tool_ui_wait({"ref": ref, "text": "edited"})["met"]


def test_duplicate_compositor_titles_refuse_even_with_one_accessible_frame(env):
    env.state["windows"].append({**env.win, "id": 99})
    error(ui.tool_ui_snapshot, {"window_id": 42}, "widget_missing")


def test_text_insert_failure_reports_partial_after_deletion(env):
    env.text.value = "original"
    env.text.insert_text = lambda *args: False
    failure = error(ui.tool_ui_action, {"ref": env.refs["Title"], "action": "set_text",
                    "text": "replacement", "expected_text": "original"}, "atspi_write_failed")
    assert failure.action_status == "partial" and env.text.value == ""


def test_worker_reset_invalidates_semantic_references(env):
    from deskwright.code_runtime import tool_desktop_exec
    tool_desktop_exec({"code": "pass", "reset": True})
    error(ui.tool_ui_inspect, {"ref": env.refs["Save"]}, "stale_observation")


def test_embedded_dialog_retains_outer_window_identity(env):
    field = Node('Folder Name', 'text')
    dialog = Node('New Folder', 'dialog', [field])
    dialog.parent = env.root
    env.root.children.append(dialog)
    snap = ui.tool_ui_snapshot({'window_id': 42, 'name': 'Folder Name'})
    ref = snap['controls'][0]['ref']
    assert ui.tool_ui_inspect({'ref': ref, 'include_text': True})['text'] == ''
    assert ui.tool_ui_action({'ref': ref, 'action': 'set_text', 'text': 'Reviewed',
                             'expected_text': ''})['verified']
    other = Node('Other', 'frame', [dialog])
    other.parent = env.app
    error(ui.tool_ui_inspect, {'ref': ref}, 'stale_observation')


def test_cyclic_ancestry_refuses(env):
    env.root.parent = env.text
    error(ui.tool_ui_inspect, {'ref': env.refs['Title']}, 'stale_observation')


def test_compact_snapshot_skips_inherited_noise_but_keeps_explicit_queries(env):
    panels = [Node('', 'panel') for _ in range(120)]
    for p in panels:
        p.actions = ['app.about']
        p.parent = env.root
    env.root.children = panels + env.root.children
    noisy = ui.tool_ui_snapshot({'window_id': 42})
    assert not noisy['complete']
    compact = ui.tool_ui_snapshot({'window_id': 42, 'compact': True})
    assert compact['complete']
    assert {n['name'] for n in compact['controls']} == {'Document', 'Save', 'Title', 'Ready'}
    labels = Node('Receipt', 'label')
    labels.parent = env.root
    env.root.children.append(labels)
    scoped = ui.tool_ui_snapshot({'window_id': 42, 'compact': True, 'role': 'label'})
    assert [n['name'] for n in scoped['controls']] == ['Receipt']
    error(ui.tool_ui_snapshot, {'window_id': 42, 'compact': 'yes'}, 'bad_args')


def test_ready_wait_and_changed_text_return_observed_result(env, monkeypatch):
    env.button.states.remove('sensitive')
    assert ui.tool_ui_wait({'ref': env.refs['Save'], 'ready': False})['met']
    monkeypatch.setattr(ui, 'pause', lambda _: env.button.states.add('sensitive'))
    assert ui.tool_ui_wait({'ref': env.refs['Save'], 'ready': True})['met']
    env.text.value = 'expression'
    monkeypatch.setattr(ui, 'pause', lambda _: setattr(env.text, 'value', '639.6'))
    result = ui.tool_ui_wait({'ref': env.refs['Title'], 'text_changed_from': 'expression'})
    assert result['text'] == '639.6'
    assert env.button.presses == 0
    error(ui.tool_ui_wait, {'ref': env.refs['Title'], 'text': '639.6', 'ready': True}, 'bad_args')
    env.text.parent = None
    error(ui.tool_ui_wait, {'ref': env.refs['Title'], 'ready': False}, 'stale_observation')


def test_portal_file_chooser_root_is_a_window_even_without_frame_role(env):
    env.root.role = 'file chooser'
    assert ui.tool_ui_inspect({'ref': env.refs['Title'], 'include_text': True})['text'] == ''
    assert ui.tool_ui_action({'ref': env.refs['Title'], 'action': 'set_text',
                             'text': '/tmp/estimate.txt', 'expected_text': ''})['verified']
    env.root.parent = None
    error(ui.tool_ui_inspect, {'ref': env.refs['Title']}, 'stale_observation')


def enable_collection(env, candidates):
    env.api.Role = NS(__enum_values__={0: 'text', 1: 'button', 2: 'frame'})
    env.api.role_get_name = lambda r: r
    env.api.StateType = NS(VISIBLE='visible', SHOWING='showing')
    env.api.StateSet = NS(new=lambda states: states)
    env.api.CollectionMatchType = NS(ALL='all', ANY='any')
    env.api.CollectionSortOrder = NS(CANONICAL='canonical')
    env.api.MatchRule = NS(new=lambda *args: args)
    calls = []
    def get_matches(rule, order, count, traverse):
        calls.append((rule, order, count, traverse))
        return candidates() if callable(candidates) else candidates
    env.root.get_collection_iface = lambda: NS(get_matches=get_matches)
    return calls


def test_collection_query_reads_current_text_without_walking_irrelevant_children(env):
    calls = enable_collection(env, [env.text])
    env.root.get_child_count = lambda: pytest.fail('walked irrelevant children')
    env.text.value = 'new value'
    result = ui.tool_ui_query({'window_id': 42, 'controls': {
        'field': {'role': 'text', 'name': 'Title', 'text': 'new value'}}})
    assert result['search_backend'] == 'collection'
    assert result['controls']['field']['text'] == 'new value'
    assert calls[0][2:] == (4001, True)
    assert calls[0][0][4] == []
    assert calls[1][2:] == (401, True)
    assert calls[1][0][4] == ['text']
    env.text.value = 'changed again'
    assert ui.tool_ui_inspect({'ref': result['controls']['field']['ref'],
                              'include_text': True})['text'] == 'changed again'


def test_collection_preserves_ambiguity_and_hidden_control_rules(env):
    duplicate = Node('Save')
    duplicate.parent = env.root
    candidates = [env.button, duplicate]
    enable_collection(env, candidates)
    args = {'window_id': 42, 'timeout_s': 0, 'controls': {
        'save': {'role': 'button', 'name': 'Save'}}}
    with pytest.raises(ToolError) as exc:
        ui.tool_ui_query(args)
    assert exc.value.code == 'ambiguous_control'
    duplicate.states.discard('showing')
    assert ui.tool_ui_query(args)['met']


def test_collection_includes_scope_itself_for_absence_and_identity(env):
    enable_collection(env, [])
    result = ui.tool_ui_query({'window_id': 42, 'controls': {
        'window': {'role': 'frame', 'name': 'Document'}}})
    assert result['controls']['window']['name'] == 'Document'
    with pytest.raises(ToolError, match='conditions not met'):
        ui.tool_ui_query({'window_id': 42, 'timeout_s': 0, 'controls': {
            'window': {'role': 'frame', 'name': 'Document', 'absent': True}}})


@pytest.mark.parametrize('failure', ['limit', 'provider'])
def test_collection_failure_falls_back_to_complete_tree(env, failure):
    def candidates():
        if failure == 'provider':
            raise RuntimeError('unsupported method')
        return [env.button] * 401
    enable_collection(env, candidates)
    result = ui.tool_ui_query({'window_id': 42, 'controls': {
        'save': {'role': 'button', 'name': 'Save'}}})
    assert result['search_backend'] == 'tree' and result['met']


def test_collection_never_accepts_a_control_outside_scope(env):
    foreign = Node('Save')
    enable_collection(env, [foreign])
    with pytest.raises(ToolError, match='outside the observed scope'):
        ui.tool_ui_query({'window_id': 42, 'controls': {
            'save': {'role': 'button', 'name': 'Save'}}})
    assert foreign.presses == 0


def test_collection_large_virtual_tree_never_runs_filtered_provider_search(env):
    calls = enable_collection(env, [env.button] * 4001)
    result = ui.tool_ui_query({'window_id': 42, 'controls': {
        'save': {'role': 'button', 'name': 'Save'}}})
    assert result['met'] and result['search_backend'] == 'tree'
    assert len(calls) == 1 and calls[0][0][4] == []
    assert env.button.presses == 0


def test_ambiguous_action_returns_current_choices_without_input(env):
    env.button.actions = ['click', 'open-menu']
    ref = ui.tool_ui_query({'window_id': 42, 'controls': {
        'save': {'role': 'button', 'name': 'Save'}}})['controls']['save']['ref']
    with pytest.raises(ToolError) as error:
        ui.tool_ui_action({'ref': ref, 'action': 'invoke'})
    assert error.value.code == 'bad_args' and error.value.action_status == 'not_started'
    assert error.value.details['actions'] == [
        {'index': 0, 'name': 'click'}, {'index': 1, 'name': 'open-menu'}]
    assert env.button.presses == 0


def test_reference_refreshes_ancestor_cache_after_container_moves_windows(env):
    container = Node('Editor', 'panel', [env.button])
    container.parent = env.root
    env.root.children = [container]
    ref = ui.tool_ui_snapshot({'window_id': 42})['controls'][2]['ref']
    other = Node('Other', 'frame', [container])
    other.parent = env.app
    cached = {'parent': env.root}
    container.get_parent = lambda: cached['parent']
    container.clear_cache_single = lambda: cached.update(parent=container.parent)
    with pytest.raises(ToolError, match='changed windows'):
        ui.tool_ui_action({'ref': ref, 'action': 'invoke'})
    assert env.button.presses == 0


def test_snapshot_refreshes_root_title_before_mapping(env):
    cached = {'name': 'Document'}
    env.root.get_name = lambda: cached['name']
    env.root.name = env.win['title'] = 'Renamed document'
    env.root.clear_cache_single = lambda: cached.update(name=env.root.name)
    result = ui.tool_ui_snapshot({'window_id': 42})
    assert result['complete'] and result['window']['title'] == 'Renamed document'


def test_collection_scope_refreshes_cached_parent_before_accepting_match(env):
    outside = Node('Other', 'frame', [env.button])
    outside.parent = env.app
    cached = {'parent': env.root}
    env.button.get_parent = lambda: cached['parent']
    env.button.clear_cache_single = lambda: cached.update(parent=env.button.parent)
    enable_collection(env, [env.button])
    with pytest.raises(ToolError, match='outside the observed scope'):
        ui.tool_ui_query({'window_id': 42, 'controls': {'save': {'role': 'button', 'name': 'Save'}}})
