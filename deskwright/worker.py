"""Persistent tool worker. Its D-Bus connections die with it, releasing held input."""
import json
import sys

from .execution import operation
from .session import start_deferred


def send(value):
    sys.__stdout__.write(json.dumps(value) + '\n')
    sys.__stdout__.flush()


def _repin():
    """Re-read a pinned headless session's address at worker start.

    The supervisor pinned its environment once, when it started; a session
    restarted since (desktop_resize, or a crash and re-ensure) has a new bus.
    A worker that inherits the old address talks to nothing.
    """
    import os

    from .session import pinned_name
    try:
        name = pinned_name()
        if not name or os.environ.get("DESKWRIGHT_HEADLESS_LAZY"):
            return
        from .headless import pin_env, status
        state = status(name)
        if state.get("running"):
            pin_env(state)
    except Exception:
        pass


def main():
    from . import resize, server
    _repin()
    # A worker that is about to exit (desktop_resize) must say so BEFORE its
    # reply, on the same pipe. Otherwise the client's next call can reach the
    # supervisor first and be written to a worker that is already leaving,
    # which came back as worker_lost on 2026-11-09.
    reply = server._respond

    def respond(msg_id, result):
        if resize.RESTART_WORKER:
            send({'retiring': True})
        reply(msg_id, result)
    server._respond = respond
    for line in sys.stdin:
        msg = json.loads(line)
        try:
            start_deferred()
            with operation(60, lambda record: send({'progress': record})):
                server.handle(msg)
        except Exception as e:
            send({'jsonrpc': '2.0', 'id': msg['id'], 'result': {
                'isError': True, 'content': [{'type': 'text', 'text': json.dumps({
                    'code': getattr(e, 'code', 'worker_error'), 'error': str(e),
                    'action_status': 'unknown'})}]}})
        if resize.RESTART_WORKER:
            # Its D-Bus connections point at the session that was just stopped.
            return


if __name__ == '__main__':
    main()
