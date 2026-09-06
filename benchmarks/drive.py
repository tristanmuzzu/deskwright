#!/usr/bin/env python3
"""Persistent MCP bridge for a model-driven, recorded desktop experiment.

Read JSON {tool, args} requests on stdin. Inline images are saved for inspection.
It executes only supplied tool calls; no model or hidden task solver lives here.
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from mcpdrv import Server  # noqa: E402

OUT=Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True,exist_ok=True)
with Server() as server, (OUT/'model-actions.jsonl').open('a') as log:
    print('READY',flush=True)
    for line in sys.stdin:
        try:
            req=json.loads(line)
            result=server.call(req['tool'],req.get('args',{}))
            data=result.pop('_image_data',None)
            if data:
                path=OUT/f'image-{time.time_ns()}.png'
                path.write_bytes(base64.b64decode(data))
                result['_saved_image']=str(path)
            if not result.get('_is_error'):
                result.pop('_text',None)
            log.write(json.dumps({'at':time.time(),'request':req,'result':result})+'\n')
            log.flush()
            print(json.dumps(result),flush=True)
        except Exception as exc:
            print(json.dumps({'bridge_error':str(exc)}),flush=True)
