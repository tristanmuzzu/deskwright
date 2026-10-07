"""Worker-local query checks; use an owned private desktop, never model timing.

Call run(desktop, baseline="/path/to/pre-change-semantic.py") inside desktop_exec
for alternating live traversal measurements. Omit baseline for functional checks.
"""
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

from deskwright import semantic


def run(desktop, baseline=None):
    root = Path(tempfile.mkdtemp(prefix='deskwright-query-fixtures-'))
    owned = []
    results = []
    try:
        files = desktop.call('launch_app', command=['nautilus', '--new-window', str(root)])['window']['id']
        owned.append(files)
        desktop.query(files, listing={'role': 'frame', 'name': root.name}, timeout_s=5)
        for index in range(3):
            began = time.monotonic()
            desktop.key('Ctrl+Shift+N', target=files)
            controls = desktop.query(files,
                field={'role': 'text', 'name': 'Folder Name', 'include_text': True},
                create={'role': 'button', 'name': 'Create'})
            desktop.call('ui_action', ref=controls['field']['ref'], action='set_text',
                         text=f'Reviewed-{index}', expected_text=controls['field']['text'])
            desktop.call('ui_wait', ref=controls['create']['ref'], ready=True)
            desktop.call('ui_action', ref=controls['create']['ref'], action='invoke')
            desktop.query(files, closed={'role': 'dialog', 'name': 'New Folder', 'absent': True})
            assert (root / f'Reviewed-{index}').is_dir()
            results.append({'case': 'create_folder', 'seconds': time.monotonic()-began, 'exact': True})
        if baseline is not None:
            # Same live tree, alternating implementations; no mutations in measurement.
            spec = importlib.util.spec_from_file_location('deskwright._query_baseline', baseline)
            old = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = old
            spec.loader.exec_module(old)
            for label in ('old', 'new', 'new', 'old', 'old', 'new'):
                fn = old.tool_ui_snapshot if label == 'old' else semantic.tool_ui_snapshot
                began = time.monotonic()
                snapshot = fn({'window_id': files, 'role': 'button', 'name': 'Main Menu', 'max_nodes': 1600, 'budget_ms': 4000})
                results.append({'case': 'filtered_snapshot', 'version': label, 'seconds': time.monotonic()-began,
                                'complete': snapshot['complete'], 'matches': len(snapshot['controls'])})
        calc = desktop.call('launch_app', command=['gnome-calculator'])['window']['id']
        owned.append(calc)
        for expression, expected in [('(125*3+79*2)*1.2', '639.6'), ('21*2', '42'), ('100/4', '25')]:
            began = time.monotonic()
            field = desktop.query(calc, field={'role': 'text', 'editable': True, 'include_text': True}, timeout_s=5)['field']
            desktop.call('ui_action', ref=field['ref'], action='set_text', text=expression, expected_text=field['text'])
            desktop.key('Enter', target=calc)
            result = desktop.query(calc, field={'role': 'text', 'editable': True, 'text': expected})['field']
            assert result['text'] == expected
            results.append({'case': 'calculator', 'seconds': time.monotonic()-began, 'exact': True})
    finally:
        for w in reversed(owned):
            desktop.call('window_manage', action='close', target=w)
            desktop.wait('window_gone', target=w, timeout=3)
    return results
