#!/usr/bin/env python3
"""Cold-click delivery and interruption witnessed by a separate GTK app."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from mcpdrv import Server  # noqa: E402

from deskwright.session import resolve_session  # noqa: E402

if not os.environ.get('DESKWRIGHT_SESSION','').startswith('headless:'):
    raise SystemExit('Use a named private desktop')
resolve_session(False)
OUT=Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True,exist_ok=False)
proc=subprocess.Popen([sys.executable,str(ROOT/'benchmarks/stroke_witness.py'),str(OUT)],
                      stderr=(OUT/'witness.log').open('w'))
report={}

def strokes():
    try:
        return json.loads((OUT/'events.json').read_text())['strokes']
    except (FileNotFoundError,json.JSONDecodeError):
        return []

try:
    with Server() as s:
        s.call('wait_for',{'condition':'window_exists','target':'Stroke Witness','timeout':10})
        w=s.call('activate_window',{'target':'Stroke Witness'})['window']
    configs=[('current',ROOT/'mcp_server.py')]
    if len(sys.argv)>2:
        configs.insert(0,('baseline',Path(sys.argv[2])))
    for label,server in configs:
        report[label]=[]
        report[label+'_keys']=[]
        for i in range(5):
            before=len(strokes())
            # Already pinned to the private bus. Avoid re-bootstrap in a
            # baseline checkout that does not preserve private HOME metadata.
            with Server(server=server,env={'DESKWRIGHT_SESSION':'primary'}) as s:
                r=s.call('pointer_click',{'x':w['x']+100+i*35,'y':w['y']+120,
                                         'expect_window':w['id'],'look':False})
                deadline=time.monotonic()+1
                while time.monotonic()<deadline and len(strokes())==before:
                    time.sleep(.02)
                report[label].append({'received':len(strokes())==before+1,
                                      'reported_error':r['_is_error'],'seconds':r['_elapsed']})
        for _ in range(5):
            key_file=OUT/'keys.json'
            before_keys=sum(e['keyval']==97 for e in json.loads(key_file.read_text())) if key_file.exists() else 0
            with Server(server=server,env={'DESKWRIGHT_SESSION':'primary'}) as s:
                result=s.call('press_keys',{'target':w['id'],'combo':'a','via':'keysym','look':False})
                time.sleep(.2)
                after_keys=sum(e['keyval']==97 for e in json.loads(key_file.read_text())) if key_file.exists() else 0
                report[label+'_keys'].append({'received_once':after_keys==before_keys+1,
                                              'seconds':result['_elapsed'], 'result':result})
    with Server() as control, Server() as drawing:
        before=len(strokes())
        result={}
        def draw():
            result.update(drawing.call('pointer_path',{'target':w['id'],
                'points':[[w['x']+100,w['y']+200],[w['x']+700,w['y']+200]],
                'duration_ms':4000,'look':False}))
        t=threading.Thread(target=draw)
        t.start()
        time.sleep(.45)
        control.call('window_manage',{'target':w['id'],'action':'move_resize',
                    'x':w['x']+30,'y':w['y']+20,'width':w['width'],'height':w['height']})
        t.join(timeout=8)
        assert not t.is_alive(), 'stroke did not stop'
        time.sleep(.2)
        observed=strokes()[before:]
        report['moved_window']={'error':result.get('_text'),
            'seconds':result.get('_elapsed'),'release_observed':bool(observed and observed[-1][-1]['kind']=='up')}
        assert result['_is_error'] and 'widget_moved' in result['_text'], result
        assert report['moved_window']['release_observed'], report
    assert all(r['received'] and not r['reported_error'] for r in report['current']), report
    assert all(r['received_once'] for r in report['current_keys']), report
    report['passed']=True
finally:
    proc.terminate()
    proc.wait(timeout=5)
    (OUT/'results.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
