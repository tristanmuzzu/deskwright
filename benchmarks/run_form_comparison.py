"""Paired scripted interface timings with the same GUI primitives and file oracle."""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from mcpdrv import Server  # noqa: E402

from deskwright.session import resolve_session  # noqa: E402

if not os.environ.get('DESKWRIGHT_SESSION', '').startswith('headless:'):
    raise SystemExit('requires private desktop')
resolve_session(False)
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=False)
saved = OUT/'form.json'
app = subprocess.Popen([sys.executable, str(ROOT/'benchmarks/contract_form.py'), str(saved)],
                       stderr=(OUT/'app.log').open('w'))
report = {'kind': 'scripted interface comparison; model round-trip time is not included',
          'trials': [], 'calls': []}


def call(s, name, args):
    r = s.call(name, args)
    data = r.pop('_image_data', None)
    if data:
        (OUT/f'view-{len(report["calls"]):03}.png').write_bytes(base64.b64decode(data))
    report['calls'].append({'tool': name, 'args': args, 'result': r})
    if r['_is_error']:
        raise AssertionError(r['_text'])
    return r


try:
    with Server(env={'DESKWRIGHT_ENABLE_EXEC': '1'}) as s:
        call(s, 'wait_for', {'condition': 'window_exists', 'target': 'Deskwright Contract Form', 'timeout': 15})
        w = call(s, 'activate_window', {'target': 'Deskwright Contract Form', 'look': False})['window']
        fields = call(s, 'ui_find', {'app': 'deskwright-contract-form', 'role': 'text'})['results']
        paths = {f['name']: f['path'] for f in fields}
        assert len(paths) == 5, fields
        buttons = {}
        for label in ['Save', 'Reopen']:
            matches = call(s, 'ui_find', {'app': 'deskwright-contract-form', 'text': label})['results']
            buttons[label] = next(b['path'] for b in matches if b.get('actions'))
        for index, mode in enumerate(['do_steps', 'desktop_exec', 'desktop_exec', 'do_steps']):
            expected = {'Name': f'Trial {index}', 'City': 'München', 'Reference': '山-42',
                        'Notes': 'Café, ready.', 'Status': 'Verified'}
            sequence = [{'do': 'set_text', 'path': paths[k], 'text': v, 'replace': True}
                        for k, v in expected.items()]
            sequence += [{'do': 'press', 'path': buttons['Save'], 'expect_name': 'Save'}]
            sequence += [{'do': 'set_text', 'path': paths[k], 'text': '', 'replace': True} for k in paths]
            sequence += [{'do': 'press', 'path': buttons['Reopen'], 'expect_name': 'Reopen'}]
            first = len(report['calls'])
            start = time.monotonic()
            if mode == 'do_steps':
                call(s, 'do_steps', {'steps': sequence, 'look': False})
                observed = {k: call(s, 'ui_read_text', {'path': p})['text'] for k, p in paths.items()}
                call(s, 'screenshot', {'window': w['id'], 'image_profile': 'original'})
            else:
                # Same primitives. Python can aggregate the readbacks and display
                # explicitly within this call, without repeating the task prompt.
                code = f"seq={sequence!r}\npaths={paths!r}\n"
                code += "for st in seq:\n    st=dict(st)\n    kind=st.pop('do')\n    desktop.call({'set_text':'ui_set_text','press':'ui_press'}[kind], **st)\n"
                code += "log({k:desktop.call('ui_read_text', path=p)['text'] for k,p in paths.items()})\n"
                code += f"display(desktop.screenshot(window={w['id']}, image_profile='original'))"
                r = call(s, 'desktop_exec', {'code': code})
                observed = json.loads(r['output'][0])
            elapsed = time.monotonic()-start
            disk = json.loads(saved.read_text())
            passed = observed == expected and disk == expected
            calls = report['calls'][first:]
            report['trials'].append({'mode': mode, 'seconds': elapsed, 'passed': passed,
                'mcp_calls': len(calls), 'images': sum(c['result']['_images'] for c in calls),
                'image_bytes': sum(c['result']['_image_bytes'] for c in calls)})
            assert passed, (observed, disk, expected)
        # Same non-editing click. Separate observation policy from execution interface.
        report['observations'] = []
        for mode in ['auto', 'compact', 'compact', 'auto']:
            args = {'x': w['x']+30, 'y': w['y']+35, 'expect_window': w['id']}
            if mode == 'compact':
                args['observation_mode'] = 'compact'
            r = call(s, 'pointer_click', args)
            report['observations'].append({'mode': mode, 'seconds': r['_elapsed'], 'images': r['_images']})
    report['passed'] = True
finally:
    app.terminate()
    app.wait(timeout=5)
    (OUT/'results.json').write_text(json.dumps(report, indent=2))
print(json.dumps({k: v for k, v in report.items() if k != 'calls'}, indent=2))
