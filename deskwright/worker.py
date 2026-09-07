"""Persistent tool worker. Its D-Bus connections die with it, releasing held input."""
import json
import sys

from .execution import operation
from .session import start_deferred


def send(value):
    sys.__stdout__.write(json.dumps(value) + '\n')
    sys.__stdout__.flush()


def main():
    from . import server
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


if __name__ == '__main__':
    main()
