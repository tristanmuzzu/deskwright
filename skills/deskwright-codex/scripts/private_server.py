#!/usr/bin/env python3
"""Launch Deskwright on a private desktop named for this task or connection.

Existing registrations and desktops are never reconfigured or stopped. The
current Codex registration allocates per connection; a host explicitly supplying
CODEX_THREAD_ID can opt into stable task identity. Startup stays deferred.
An empty connection-owned desktop is reclaimed on normal disconnect; windows
are preserved for recovery rather than closed implicitly.
"""
import argparse
import hashlib
import json
import os
import runpy
import signal
import sys
import uuid
from pathlib import Path


def allocation(task_id, connection_id, data_root):
    identity = 'task:' + task_id if task_id else 'connection:' + connection_id
    name = 'codex-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
    return {'session': 'headless:' + name, 'name': name,
            'private_home': str(data_root / name),
            'ownership': 'task' if task_id else 'connection'}


def cleanup_connection(plan, *, status=None, pin=None, windows=None, stop=None):
    """Reclaim only this connection's empty desktop, preserving uncertain state."""
    if plan['ownership'] != 'connection':
        return 'retained-task-session'
    if status is None:
        from deskwright.headless import pin_env as pin
        from deskwright.headless import status, stop
        from deskwright.shell import list_windows as windows
    try:
        state = status(plan['name'])
        if not state.get('running'):
            return 'not-running'
        pin(state)
        if windows():
            return 'retained-open-windows'
        stop(plan['name'])
        return 'stopped-empty-session'
    except Exception as error:
        print('Deskwright cleanup retained session: ' + type(error).__name__, file=sys.stderr)
        return 'retained-uncertain-state'


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--describe', action='store_true')
    args = parser.parse_args()
    plan = allocation(os.environ.get('CODEX_THREAD_ID'), uuid.uuid4().hex,
                      Path.home() / '.local/share/deskwright/codex-desktops')
    if args.describe:
        print(json.dumps(dict(plan, diagnostic_only=True, note='Preview allocation only; this does not identify an existing server.')))
        return
    entry = args.checkout.resolve() / 'mcp_server.py'
    if not entry.is_file():
        parser.error('the configured Deskwright checkout has no mcp_server.py')
    os.environ['DESKWRIGHT_SESSION'] = plan['session']
    os.environ['DESKWRIGHT_HEADLESS_HOME'] = plan['private_home']
    # The package preserves host registry access when pinning a private home.
    # No compositor is started here: the existing lazy-start contract owns it.
    sys.path.insert(0, str(entry.parent))
    sys.argv = [str(entry)]
    print('Deskwright private allocation: ' + json.dumps(plan), file=sys.stderr)
    signal.signal(signal.SIGTERM, terminate)
    try:
        runpy.run_path(str(entry), run_name='__main__')
    finally:
        result = cleanup_connection(plan)
        print('Deskwright private cleanup: ' + result + ' ' + plan['name'], file=sys.stderr)


if __name__ == '__main__':
    main()
