"""Private live regression measurements, invoked in a fresh desktop_exec worker.

Saved baseline modules keep pre-change behavior without modifying the checkout.
All UI operations use the same guarded desktop.call. Filesystem use creates
fixtures and independently verifies outcomes, never completes an application task.
"""
import importlib.util
import json
import time
from pathlib import Path

from deskwright import input as improved_input
from deskwright import semantic as improved_ui
from deskwright import server


def run(desktop, baseline='/tmp/deskwright-hybrid-baseline'):
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='deskwright-hybrid-live-'))
    results, owned = [], []

    def old_module(name):
        spec = importlib.util.spec_from_file_location(
            'deskwright.baseline_' + name, str(Path(baseline)/'deskwright'/f'{name}.py'))
        mod = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    old_input, old_ui = old_module('input'), old_module('semantic')
    originals = {name: server.HANDLERS[name] for name in
                 ['type_text', 'ui_snapshot', 'ui_inspect', 'ui_action', 'ui_wait']}

    def version(old):
        ui = old_ui if old else improved_ui
        inp = old_input if old else improved_input
        for name in originals:
            server.HANDLERS[name] = getattr(inp if name == 'type_text' else ui, 'tool_' + name)

    def until(fn, seconds=4):
        deadline = time.monotonic()+seconds
        while True:
            result = fn()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise AssertionError('live condition timed out')
            desktop.sleep(.02)

    def refs(window, role=None, name=None, compact=False):
        args = {'window_id': window, 'budget_ms': 4000, 'max_nodes': 1600, 'compact': compact}
        if role:
            args['role'] = role
        if name:
            args['name'] = name
        return desktop.call('ui_snapshot', **args)

    def launch(command):
        w = desktop.call('launch_app', command=command)['window']['id']
        owned.append(w)
        desktop.call('activate_window', target=w)
        return w

    def attempt(name, old, fn):
        start = time.monotonic()
        try:
            value = fn()
            result = {'case': name, 'version': 'before' if old else 'after',
                      'ok': True, 'result': value}
        except Exception as exc:
            result = {'case': name, 'version': 'before' if old else 'after',
                      'ok': False, 'code': getattr(exc, 'code', type(exc).__name__)}
        result['seconds'] = time.monotonic()-start
        results.append(result)
        return result

    try:
        files = launch(['nautilus', '--new-window', str(root)])
        desktop.call('window_layout', action='move_resize', target=files,
                     x=40, y=40, width=1000, height=850)
        for old in (True, False):
            version(old)
            attempt('files_snapshot', old, lambda old=old: {
                key: value for key, value in refs(files, compact=not old).items()
                if key in ('complete', 'limits_hit', 'visited', 'elapsed_ms')})
            snapshot = refs(files, compact=not old)
            results[-1]['controls'] = len(snapshot['controls'])
            results[-1]['json_bytes'] = len(json.dumps(snapshot).encode())
            desktop.call('press_keys', target=files, combo='Ctrl+Shift+N')
            def dialog_field():
                nodes = refs(files, role='text', name='Folder Name')['controls']
                return nodes[0]['ref'] if nodes else None
            field = until(dialog_field)
            attempt('embedded_dialog_read', old, lambda field=field: desktop.call(
                'ui_inspect', ref=field, include_text=True)['text'])
            if not old:
                desktop.call('ui_action', ref=field, action='set_text', text='Reviewed', expected_text='')
                create = refs(files, role='button', name='Create')['controls'][0]['ref']
                desktop.call('ui_wait', ref=create, ready=True)
                desktop.call('ui_action', ref=create, action='invoke')
                until(lambda: (root/'Reviewed').is_dir())
            else:
                desktop.call('press_keys', target=files, combo='Escape')
            desktop.sleep(.1)
        # Same location selection and destination in both versions. Do not type
        # a second time on a verification failure; inspect with the exact oracle.
        for old in (True, False, False, True):
            version(False)
            desktop.call('press_keys', target=files, combo='Ctrl+L')
            loc = refs(files, role='text')['controls']
            loc = next(n['ref'] for n in loc if n.get('editable'))
            before = desktop.call('ui_inspect', ref=loc, include_text=True)['text']
            desktop.call('ui_action', ref=loc, action='set_text', text=str(root), expected_text=before)
            desktop.call('press_keys', target=files, combo='Ctrl+A')
            node = improved_ui.REFS[loc].node
            try:
                iface = node.get_text_iface()
                selection_debug = {'span': improved_input._edit_span(node, len(str(root))), 'count': iface.get_n_selections(), 'caret': improved_input._atspi().Text.get_caret_offset(iface)}
            except Exception as exc:
                selection_debug = {'error': str(exc)}
            version(old)
            attempt('selected_path_replacement', old, lambda: desktop.call(
                'type_text', target=files, text=str(root/'Reviewed'))['verified'])
            version(False)
            actual = desktop.call('ui_inspect', ref=loc, include_text=True)['text']
            results[-1]['selection_debug'] = selection_debug
            results[-1]['exact_or_selected_completion'] = actual.rstrip('/') == str(root/'Reviewed')
            desktop.call('press_keys', target=files, combo='Escape')
        version(False)
        calculator = launch(['gnome-calculator'])
        for old in (True, False, False, True):
            version(False)
            nodes = refs(calculator, role='text')['controls']
            ref = next(n['ref'] for n in nodes if n.get('editable'))
            previous = desktop.call('ui_inspect', ref=ref, include_text=True)['text']
            desktop.call('ui_action', ref=ref, action='set_text', text='', expected_text=previous)
            version(old)
            attempt('calculator_normalized_typing', old, lambda: desktop.call(
                'type_text', target=calculator, text='(125*3+79*2)*1.2',
                expected_after='(125\u00d73+79\u00d72)\u00d71.2')['verified'])
            version(False)
            actual = desktop.call('ui_inspect', ref=ref, include_text=True)['text']
            assert actual == '(125\u00d73+79\u00d72)\u00d71.2', actual
            desktop.call('press_keys', target=calculator, combo='Enter')
            wait = desktop.call('ui_wait', ref=ref, text_changed_from=actual)
            assert wait['text'] == '639.6', wait
            results[-1]['calculated'] = wait['text']
    finally:
        server.HANDLERS.update(originals)
        for window in reversed(owned):
            desktop.call('window_manage', action='close', target=window)
    return {'samples': results, 'scope': 'Same private session, worker-local tool timing; no model latency.'}


def typing_speed(desktop):
    """Alternating ASCII delays; native Unicode verified separately."""
    Path('/tmp/deskwright-hybrid-speed.txt').write_text('')
    window = desktop.call('launch_app', command=['gnome-text-editor', '--new-window', str(Path('/tmp/deskwright-hybrid-speed.txt'))])['window']['id']
    desktop.call('activate_window', target=window)
    samples = []
    text = 'Mixed keyboard input yz AZ 0123456789.\nSecond line with punctuation!\n' * 2
    try:
        for delay in (20, 8, 8, 20, 20, 8):
            tree = desktop.call('ui_snapshot', window_id=window, role='text', max_nodes=1600, budget_ms=4000)
            ref = next(n['ref'] for n in tree['controls'] if n.get('editable'))
            before = desktop.call('ui_inspect', ref=ref, include_text=True)['text']
            desktop.call('ui_action', ref=ref, action='set_text', text='', expected_text=before)
            start = time.monotonic()
            out = desktop.call('type_text', target=window, text=text, key_delay_ms=delay)
            elapsed = time.monotonic()-start
            actual = desktop.call('ui_inspect', ref=ref, include_text=True)['text']
            assert actual == text, (delay, actual)
            samples.append({'delay_ms': delay, 'seconds': elapsed, 'characters': len(text), 'exact': True,
                            'verified': out['verified']})
        unicode_text = 'Café München 山 — native text.\n'
        desktop.call('ui_action', ref=ref, action='set_text', text=unicode_text, expected_text=text)
        assert desktop.call('ui_inspect', ref=ref, include_text=True)['text'] == unicode_text
        samples.append({'case': 'native_unicode', 'exact': True})
        desktop.call('ui_action', ref=ref, action='set_text', text='', expected_text=unicode_text)
        desktop.call('press_keys', target=window, combo='Ctrl+S')
        # The test buffer is now empty; close without changing any saved document.
        desktop.call('press_keys', target=window, combo='Ctrl+W')
    finally:
        # Only this private test window is owned here.
        if any(w['id'] == window for w in desktop.call('list_windows')['windows']):
            desktop.call('window_manage', target=window, action='close')
    return samples


def real_workflow(desktop):
    """Reproduce the estimate task using short, condition-checked hybrid operations."""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='deskwright-hybrid-workflow-'))
    original = ('Estimate draft\n\nProject: Northstar\nService A: 3 units at EUR 125.00\n'
                'Service B: 2 units at EUR 79.00\nTax: 20%\nTotal: PENDING\nStatus: draft\n')
    (root/'estimate-draft.txt').write_text(original)
    events, owned = [], []
    start = time.monotonic()

    def call(tool, **args):
        began = time.monotonic()
        out = desktop.call(tool, **args)
        events.append({'tool': tool, 'seconds': time.monotonic()-began})
        return out

    def until(fn, timeout=5):
        end = time.monotonic()+timeout
        while True:
            out = fn()
            if out:
                return out
            if time.monotonic() >= end:
                raise AssertionError('workflow condition timed out')
            desktop.sleep(.03)

    def window(wm=None, title=None):
        return next((w['id'] for w in call('list_windows')['windows']
                     if (wm is None or w['wm_class'] == wm) and (title is None or w['title'] == title)), None)

    def control(w, role, name=None):
        snap = call('ui_snapshot', window_id=w, role=role, **({'name': name} if name else {}),
                    max_nodes=1600, budget_ms=4000)
        nodes = [n for n in snap['controls'] if 'showing' in n['states'] and (name is None or n['name'] == name)]
        if role == 'text' and name is None:
            nodes = [n for n in nodes if n.get('editable')]
        return nodes[0]['ref'] if len(nodes) == 1 else None

    def file_visible(w, name):
        return any(name in n['name'] and 'showing' in n['states'] for n in call(
            'ui_snapshot', window_id=w, name=name, max_nodes=1600, budget_ms=4000)['controls'])

    def set_text(ref, value):
        old = call('ui_inspect', ref=ref, include_text=True)['text']
        call('ui_action', ref=ref, action='set_text', text=value, expected_text=old)

    def launch(argv):
        w = call('launch_app', command=argv)['window']['id']
        owned.append(w)
        call('activate_window', target=w)
        return w

    def navigate(w, path):
        call('press_keys', target=w, combo='Ctrl+L')
        call('type_text', target=w, text=str(path))
        call('press_keys', target=w, combo='Enter')

    try:
        files = launch(['nautilus', '--new-window', str(root)])
        call('press_keys', target=files, combo='Ctrl+Shift+N')
        field = until(lambda: control(files, 'text', 'Folder Name'))
        set_text(field, 'Reviewed')
        create = until(lambda: control(files, 'button', 'Create'))
        call('ui_wait', ref=create, ready=True)
        call('ui_action', ref=create, action='invoke')
        until(lambda: (root/'Reviewed').is_dir())
        editor = launch(['gnome-text-editor', '--new-window', '--ignore-session'])
        call('activate_window', target=files)
        navigate(files, root/'estimate-draft.txt')
        until(lambda: file_visible(files, 'estimate-draft.txt'))
        call('press_keys', target=files, combo='Escape')
        call('press_keys', target=files, combo='Enter')
        editor = until(lambda: window(wm='org.gnome.TextEditor', title=f'estimate-draft.txt ({root}) - Text Editor'))
        call('activate_window', target=editor)
        textref = until(lambda: control(editor, 'text'))
        read = call('ui_inspect', ref=textref, include_text=True)['text']
        assert read+'\n' == original
        reviewed = read.replace('Estimate draft', 'Reviewed estimate').replace('Status: draft','Status: reviewed')
        set_text(textref, reviewed)
        calc = launch(['gnome-calculator'])
        calc_ref = until(lambda: control(calc, 'text'))
        expression = '(125*3+79*2)*1.2'
        set_text(calc_ref, expression)
        call('press_keys', target=calc, combo='Enter')
        value = call('ui_wait', ref=calc_ref, text_changed_from=expression)['text']
        assert value == '639.6'
        call('activate_window', target=editor)
        final = reviewed.replace('Total: PENDING', f'Total: EUR {float(value):.2f}')
        set_text(until(lambda: control(editor, 'text')), final)
        call('press_keys', target=editor, combo='Ctrl+Shift+S')
        save = until(lambda: window(title='Save a File'))
        field = until(lambda: control(save, 'text'))
        save_window = next(w for w in call('list_windows')['windows'] if w['id'] == save)
        if save_window['wm_class'] == 'xdg-desktop-portal-gtk':
            set_text(field, str(root/'Reviewed'/'estimate.txt'))
        else:
            set_text(field, 'estimate.txt')
            navigate(save, root/'Reviewed')
        button = until(lambda: control(save, 'button', 'Save') or control(save, 'push button', 'Save'))
        call('ui_wait', ref=button, ready=True)
        call('ui_action', ref=button, action='invoke')
        until(lambda: (root/'Reviewed'/'estimate.txt').exists())
        call('activate_window', target=files)
        navigate(files, root/'Reviewed'/'estimate.txt')
        until(lambda: file_visible(files, 'estimate.txt'))
        call('press_keys', target=files, combo='Escape')
        call('press_keys', target=files, combo='F2')
        rename = until(lambda: control(files, 'text'))
        set_text(rename, 'approved-estimate.txt')
        call('press_keys', target=files, combo='Enter')
        until(lambda: (root/'Reviewed'/'approved-estimate.txt').exists())
        call('press_keys', target=files, combo='Alt+Return')
        until(lambda: control(files, 'label', 'Plain text document'))
        call('press_keys', target=files, combo='Escape')
        call('press_keys', target=files, combo='Enter')
        until(lambda: any('approved-estimate.txt' in (w['title'] or '') for w in call('list_windows')['windows']
                         if w['wm_class'] == 'org.gnome.TextEditor'))
        call('activate_window', target=editor)
        actual = call('ui_inspect', ref=until(lambda: control(editor, 'text')), include_text=True)['text']
        assert actual == final
        elapsed = time.monotonic()-start
        assert (root/'estimate-draft.txt').read_text() == original
        assert (root/'Reviewed'/'approved-estimate.txt').read_text() == final+'\n'
        return {'seconds': elapsed, 'calls': len(events), 'events': events, 'exact': True,
                'original_unchanged': True, 'directory': str(root),
                'scope': 'Scripted known workflow with native observations and keyboard navigation; excludes model latency.'}
    finally:
        Path('/tmp/deskwright-hybrid-workflow-progress.json').write_text(json.dumps({'directory':str(root),'events':events}))
        for w in reversed(owned):
            if any(n['id'] == w for n in desktop.call('list_windows')['windows']):
                desktop.call('window_manage', action='close', target=w)
                desktop.wait('window_gone', target=w, timeout=2)
