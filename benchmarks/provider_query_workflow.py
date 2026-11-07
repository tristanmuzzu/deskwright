"""Compare query implementations in a real, disposable Writer editing workflow.

Run in desktop_exec on an owned private desktop after creating a synthetic ODT.
Both variants use identical observed dialog labels and guarded actions. Timings
include application operations and file verification, but exclude model latency.
"""
import importlib.util
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from deskwright import semantic, server


def read_odt(path):
    with zipfile.ZipFile(path) as archive:
        tree = ET.fromstring(archive.read('content.xml'))
    return [''.join(e.itertext()) for e in tree.iter() if e.tag in {
        '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p',
        '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}h'}]


def run(desktop, writer, document, expected, baseline, initial_day='Wednesday'):
    if not os.environ.get('DESKWRIGHT_HEADLESS'):
        raise RuntimeError('requires an owned private desktop and disposable document')
    document = Path(document)
    assert read_odt(document) == expected
    spec = importlib.util.spec_from_file_location('deskwright._workflow_baseline', baseline)
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    spec.loader.exec_module(old)
    names = ['ui_query', 'ui_inspect', 'ui_action', 'ui_wait']
    saved = {name: server.HANDLERS[name] for name in names}
    results, day = [], initial_day
    try:
        for index, method in enumerate(['previous', 'new', 'new', 'previous',
                                         'new', 'previous', 'previous', 'new']):
            implementation = semantic if method == 'new' else old
            for name in names:
                server.HANDLERS[name] = getattr(implementation, 'tool_' + name)
            next_day = ['Thursday', 'Monday', 'Tuesday', 'Friday',
                        'Monday', 'Thursday', 'Friday', 'Wednesday'][index]
            began = time.monotonic()
            before = [w['id'] for w in desktop.call('list_windows')['windows']]
            desktop.key('Ctrl+H', target=writer)
            new = desktop.wait('window_new', since=before, timeout=3)
            assert new['matched_count'] == 1
            dialog = new['matched'][0]['id']
            panels = desktop.query(dialog, search={'role': 'panel', 'name': 'Search For'},
                                   replace={'role': 'panel', 'name': 'Replace With'})
            for panel, value in [('search', day), ('replace', next_day)]:
                field = desktop.query(dialog, within=panels[panel]['ref'],
                    field={'role': 'text', 'editable': True, 'include_text': True})['field']
                desktop.call('ui_action', ref=field['ref'], action='set_text',
                             text=value, expected_text=field['text'])
            button = desktop.query(dialog,
                button={'role': 'button', 'name': 'Replace All', 'ready': True})['button']
            desktop.call('ui_action', ref=button['ref'], action='invoke')
            desktop.key('Escape', target=dialog)
            desktop.wait('window_gone', target=dialog, timeout=3)
            desktop.query(writer, button={'role': 'button', 'name': 'Save', 'ready': True})
            # This toolbar exposes both press and push without descriptions.
            # Use the known Save shortcut after checking its readiness.
            desktop.key('Ctrl+S', target=writer)
            expected = [text.replace(day, next_day) for text in expected]
            deadline = time.monotonic() + 3
            while True:
                try:
                    correct = read_odt(document) == expected
                except (OSError, zipfile.BadZipFile, ET.ParseError):
                    correct = False
                if correct or time.monotonic() >= deadline:
                    break
                desktop.sleep(.03)
            assert correct, 'saved document differs; no action will be replayed'
            shot = desktop.screenshot(window=writer)
            results.append({'method': method, 'seconds': time.monotonic()-began,
                            'replacement': next_day, 'exact_artifact': correct,
                            'screenshot_bytes': shot['bytes']})
            day = next_day
    finally:
        server.HANDLERS.update(saved)
    return results
