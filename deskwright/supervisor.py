"""Responsive MCP transport supervising a single persistent desktop worker.

Hard deadlines/cancellation kill the worker process group and invalidate Python
globals/observation IDs. The parent never owns held compositor input. This uses
the caller's OS permissions; it does not create an additional Python sandbox.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .execution import clock


def serve():
    from . import server
    events = queue.Queue()
    worker = None
    active = None
    trace = []
    trace_dropped = 0
    deadline = 0

    def reader(stream, source):
        try:
            for line in stream:
                events.put((source, line))
        except (ValueError, OSError):
            pass  # The supervisor closed a stopped worker's stream.
        finally:
            events.put((source, None))

    threading.Thread(target=reader, args=(sys.stdin, 'client'), daemon=True).start()

    def stop():
        nonlocal worker
        if worker:
            if worker.poll() is None:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            worker.wait(timeout=5)
            worker.stdin.close()
            worker.stdout.close()
            worker = None

    def fail(code, message):
        nonlocal active
        stop()
        server._respond(active['id'], {'isError': True, 'content': [{
            'type': 'text', 'text': json.dumps({'code': code, 'error': message,
                'action_status': 'unknown', 'session_reset': True,
                'executed_prefix': trace,
                'omitted_progress_events': trace_dropped,
                'recovery': 'Worker stopped. Variables and observations invalidated. Inspect partial effects before retrying.'})}]})
        active = None

    try:
        while True:
            if active and clock() >= deadline:
                fail('timeout', '60-second execution deadline reached')
            try:
                source, line = events.get(timeout=.05)
            except queue.Empty:
                continue
            if source == 'client':
                if line is None:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get('method') == 'notifications/cancelled':
                    if active and (msg.get('params') or {}).get('requestId') == active['id']:
                        fail('cancelled', 'caller cancelled desktop execution')
                elif msg.get('method') == 'tools/call':
                    if active:
                        server._error(msg.get('id'), -32000, 'desktop is busy; serialize calls')
                        continue
                    if worker is None:
                        env = dict(os.environ)
                        root = str(Path(__file__).resolve().parent.parent)
                        env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
                        worker = subprocess.Popen([sys.executable, '-m', 'deskwright.worker'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                            text=True, bufsize=1, env=env, start_new_session=True)
                        threading.Thread(target=reader, args=(worker.stdout, worker.pid), daemon=True).start()
                    active, trace, deadline = msg, [], clock() + 60.25
                    trace_dropped = 0
                    try:
                        worker.stdin.write(json.dumps(msg) + '\n')
                        worker.stdin.flush()
                    except (BrokenPipeError, OSError):
                        fail('worker_lost', 'desktop worker exited before accepting the call')
                else:
                    server.handle(msg)
            elif worker and source == worker.pid:
                if line is None:
                    if active:
                        fail('worker_lost', 'desktop worker exited unexpectedly')
                    else:
                        stop()
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    if active:
                        fail('worker_protocol', 'worker produced malformed output')
                    continue
                if 'progress' in result:
                    trace.append(result['progress'])
                    trace_dropped += max(0, len(trace) - 512)
                    trace[:] = trace[-512:]
                elif active and result.get('id') == active['id']:
                    if clock() >= deadline - .25:
                        fail('timeout', '60-second execution deadline reached')
                        continue
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    active = None
    finally:
        stop()
    return 0
