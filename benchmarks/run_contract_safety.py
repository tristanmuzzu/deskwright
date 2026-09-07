"""Live private-desktop receiver evidence for cancellation and image mapping."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from mcpdrv import Server  # noqa: E402

from deskwright.session import resolve_session  # noqa: E402

if not os.environ.get('DESKWRIGHT_SESSION', '').startswith('headless:'):
    raise SystemExit('requires a named private desktop')
resolve_session(False)
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=False)
source = (ROOT/'benchmarks/stroke_witness.py').read_text()
source = source.replace("{'keyval':event.keyval,'at':time.monotonic()}",
                        "{'keyval':event.keyval,'kind':str(event.type),'at':time.monotonic()}")
source = source.replace("win.connect('key-press-event',key_event)",
                        "win.connect('key-press-event',key_event)\nwin.connect('key-release-event',key_event)")
(OUT/'receiver.py').write_text(source)
receiver = subprocess.Popen([sys.executable, str(OUT/'receiver.py'), str(OUT)],
                            stderr=(OUT/'receiver.log').open('w'))
report = {'kind': 'live receiver tests, not a model capability score', 'checks': []}


def events(name):
    try:
        return json.loads((OUT/name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def require(condition, name, detail):
    report['checks'].append({'name': name, 'passed': bool(condition), 'detail': detail})
    (OUT/'results.json').write_text(json.dumps(report, indent=2))
    assert condition, (name, detail)


def checked(s, tool, args):
    r = s.call(tool, args)
    if r['_is_error']:
        raise AssertionError(r['_text'])
    return r


try:
    with Server(env={'DESKWRIGHT_ENABLE_EXEC': '1'}) as s:
        checked(s, 'wait_for', {'condition': 'window_exists', 'target': 'Stroke Witness', 'timeout': 15})
        w = checked(s, 'activate_window', {'target': 'Stroke Witness', 'look': False})['window']
        x, y = w['x']+130, w['y']+180
        # Capture is cropped and resized. Input uses image coordinates only.
        shot = checked(s, 'screenshot', {'region': {'x': x-100, 'y': y-100, 'width': 333, 'height': 251},
                                        'scale': .5, 'image_profile': 'original'})
        f = shot['observation']
        px, py = 100*f['image_size'][0]/333, 100*f['image_size'][1]/251
        checked(s, 'pointer_click', {'x': px, 'y': py, 'expect_window': w['id'],
                                    'observation_id': f['id'], 'look': False})
        time.sleep(.2)
        observed = events('events.json')
        down = observed['strokes'][-1][0]
        decoration = w['height']-observed['canvas_size'][1]
        error = [abs(w['x']+down['local_x']-x), abs(w['y']+decoration+down['local_y']-y)]
        require(max(error) <= 1, 'crop/resize input reaches receiver', {'error_px': error})
        checked(s, 'pointer_drag', {'from_x': px, 'from_y': py,
                'to_x': (190*f['image_size'][0]/333), 'to_y': (145*f['image_size'][1]/251),
                'expect_window': w['id'], 'observation_id': f['id'], 'look': False})
        time.sleep(.2)
        last = events('events.json')['strokes'][-1][-1]
        error = [abs(w['x']+last['local_x']-(x+90)),
                 abs(w['y']+decoration+last['local_y']-(y+45))]
        require(max(error) <= 1, 'crop/resize drag reaches receiver', {'error_px': error})
        for tool, args, expected in [
            ('pointer_path', {'target': w['id'], 'points': [[x,y],[x+400,y]], 'duration_ms': 8000, 'look': False}, 'mouse'),
            ('hold_key', {'target': w['id'], 'key': 'shift', 'seconds': 8, 'look': False}, 'key')]:
            before = len(events('raw-events.json')) if expected == 'mouse' else len(events('keys.json'))
            timer = threading.Timer(.65, lambda: s._notify('notifications/cancelled', {'requestId': s._id}))
            timer.start()
            started = time.monotonic()
            r = s.call(tool, args)
            timer.join()
            time.sleep(.3)
            received = (events('raw-events.json') if expected == 'mouse' else events('keys.json'))[before:]
            released = any(e['kind'] == 'up' for e in received) if expected == 'mouse' else any(e['kind'] == '9' for e in received)
            require(r['_is_error'] and 'cancelled' in r['_text'] and released,
                    f'cancel releases {expected}', {'seconds': time.monotonic()-started,
                    'released': released, 'result': r['_text'], 'events': received[-4:]})
        # After worker reset the old coordinate frame must never work.
        r = s.call('pointer_click', {'x': px, 'y': py, 'expect_window': w['id'],
                   'observation_id': f['id'], 'look': False})
        require(r['_is_error'] and 'stale_observation' in r['_text'], 'reset invalidates observation', r['_text'])
        require(checked(s, 'desktop_exec', {'code': 'counter=41'})['all_ok'], 'code initialization', {})
        r = checked(s, 'desktop_exec', {'code': 'counter+=1\nlog(counter)'})
        require(r['output'] == ['42'], 'state persists between calls', r['output'])
        # Independent worker death, not a normal exception/finally block.
        r = s.call('desktop_exec', {'code': 'import os\nos._exit(17)'})
        require(r['_is_error'] and 'session_reset' in r['_text'], 'worker loss is explicit', r['_text'])
        r = checked(s, 'desktop_exec', {'code': 'log("counter" in globals())'})
        require(r['output'] == ['false'], 'worker loss clears code state', r['output'])
        if '--extended' in sys.argv:
            before = len(events('raw-events.json'))
            code = f"from deskwright.input import _pointer\np=_pointer()\np.move_to({x},{y})\ndesktop.sleep(.2)\np.button('left', True)\ndesktop.sleep(.2)\nimport os\nos._exit(19)"
            r = s.call('desktop_exec', {'code': code})
            time.sleep(.3)
            received = events('raw-events.json')[before:]
            require(r['_is_error'] and 'worker_lost' in r['_text'] and
                    any(e['kind'] == 'up' for e in received), 'worker crash releases mouse',
                    {'result': r['_text'], 'events': received[-4:]})
            checked(s, 'desktop_exec', {'code': 'before_deadline=42'})
            before = len(events('keys.json'))
            started = time.monotonic()
            r = s.call('hold_key', {'target': w['id'], 'key': 'shift', 'seconds': 70, 'look': False})
            elapsed = time.monotonic() - started
            time.sleep(.3)
            received = events('keys.json')[before:]
            require(r['_is_error'] and 'timeout' in r['_text'] and 'session_reset' in r['_text'] and
                    any(e['kind'] == '9' for e in received) and elapsed < 63,
                    '60-second deadline releases key and resets',
                    {'seconds': elapsed, 'result': r['_text'], 'events': received[-4:]})
            r = checked(s, 'desktop_exec', {'code': 'log("before_deadline" in globals())'})
            require(r['output'] == ['false'], 'deadline clears code state', r['output'])
    report['passed'] = True
finally:
    receiver.terminate()
    receiver.wait(timeout=5)
    (OUT/'results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
