#!/usr/bin/env python3
"""Scripted MCP integration benchmark; separate from model task success.

Runs only in a named private session. Records input independently in GTK.
Usage: DESKWRIGHT_SESSION=headless:astra-bench python3 benchmarks/run_strokes.py OUTPUT
"""
import base64
import json
import math
import os
import statistics
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from mcpdrv import Server  # noqa: E402

from deskwright.session import resolve_session  # noqa: E402

if not os.environ.get('DESKWRIGHT_SESSION', '').startswith('headless:'):
    raise SystemExit('Use a named private desktop')
resolve_session(False)
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
if (OUT/'events.json').exists():
    raise SystemExit('Use a fresh output directory so evidence is never overwritten')
proc = subprocess.Popen([sys.executable, str(ROOT/'benchmarks/stroke_witness.py'), str(OUT)],
                        stdout=subprocess.DEVNULL, stderr=(OUT/'witness.log').open('w'))
report = {'kind':'scripted MCP integration, not a model/native comparison', 'calls':[]}

def call(s, name, args):
    result = s.call(name, args)
    image = result.pop('_image_data', None)
    if image:
        (OUT/'last-screen.png').write_bytes(base64.b64decode(image))
    report['calls'].append({'tool':name, 'args':args, 'result':result})
    if result.get('_is_error') or result.get('_error'):
        raise RuntimeError(f'{name}: {result.get("_text", result)}')
    return result

try:
    with Server() as s:
        call(s,'wait_for',{'condition':'window_exists','target':'Deskwright Stroke Witness','timeout':15})
        windows=call(s,'list_windows',{})['windows']
        w=next(w for w in windows if 'Stroke Witness' in w['title'])
        w=call(s,'activate_window',{'target':w['id']})['window']
        x,y = w['x']+100,w['y']+120
        tests = [
            [(x,y),(x+350,y),(x+350,y+150),(x,y+150)],
            [(x+180+150*math.cos(i*2*math.pi/120), y+180+100*math.sin(i*2*math.pi/120)) for i in range(121)],
            [(x+i*5,y+100+70*math.sin(i/8)) for i in range(101)],
        ]
        report['strokes']=[]
        for index,points in enumerate(tests):
            result=call(s,'pointer_path',{'target':w['id'],'points':points,'duration_ms':1800,'look':False})
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if (OUT/'events.json').exists():
                    witness=json.loads((OUT/'events.json').read_text())
                    if len(witness['strokes'])>=index+1:
                        break
                time.sleep(.02)
            else:
                raise AssertionError('GTK did not observe button release')
            actual=witness['strokes'][index]
            # Wayland deliberately does not expose global coordinates to
            # clients: GDK's x_root/y_root are synthetic. Compare the witness's
            # canvas-local coordinates with the observed compositor geometry.
            decoration=w['height']-witness['canvas_size'][1]
            received=[(w['x']+e['local_x'],w['y']+decoration+e['local_y']) for e in actual]
            def distance_to_segment(p, a, b):
                vx,vy=b[0]-a[0],b[1]-a[1]
                length=vx*vx+vy*vy
                t=max(0,min(1,((p[0]-a[0])*vx+(p[1]-a[1])*vy)/length)) if length else 0
                return math.dist(p,(a[0]+t*vx,a[1]+t*vy))
            worst=max(min(distance_to_segment(p,a,b) for a,b in pairwise(received)) for p in points)
            ends=[math.dist(points[0],received[0]), math.dist(points[-1],received[-1])]
            assert max(ends)<2, ends
            assert worst<4, worst  # coalescing may skip points, expose error rather than claim 120Hz delivery
            report['strokes'].append({'requested_vertices':len(points),'received_events':len(actual),
                'max_distance_to_received_polyline_px':worst,'endpoint_errors_px':ends,
                'observed_ms':round((actual[-1]['at']-actual[0]['at'])*1000),
                'tool_ms':round(result['_elapsed']*1000)})
        call(s,'screenshot',{'image_profile':'original','path':str(OUT/'desktop.png')})
        report['images']={}
        for profile in ('legacy','balanced','original'):
            samples=[]
            for _ in range(5):
                r=call(s,'screenshot',{'image_profile':profile,'path':str(OUT/f'{profile}.png')})
                samples.append(r['_elapsed']*1000)
            report['images'][profile]={'median_ms':statistics.median(samples),'samples_ms':samples,
                'inline_bytes':r['_image_bytes'],'shown':r.get('shown')}
        report['passed']=True
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    (OUT/'results.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='calls'},indent=2))
