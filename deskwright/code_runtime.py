"""Opt-in persistent Python with explicit observations over guarded desktop tools."""
from __future__ import annotations

import contextlib
import io
import json

from .errors import ToolError
from .execution import execute, pause

GLOBALS = {}
OUTPUT = []
IMAGES = []


def log(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if sum(map(len, OUTPUT)) + len(text) > 32_000:
        raise ToolError('code output exceeds 32000 characters; return a concise summary', code='bad_args')
    OUTPUT.append(text)


def display(observation):
    # Only captured tool images: display is not an image renderer or file loader.
    from .capture import _INLINE_KEY
    if not isinstance(observation, dict) or _INLINE_KEY not in observation:
        raise ToolError('display expects desktop.screenshot() result', code='bad_args')
    if len(IMAGES) >= 4:
        raise ToolError('at most four displayed observations per call', code='bad_args')
    IMAGES.append(observation[_INLINE_KEY])
    log({k: v for k, v in observation.items() if k != _INLINE_KEY})


class Desktop:
    def call(self, tool, **args):
        from .server import HANDLERS
        if tool not in HANDLERS or tool == 'desktop_exec':
            raise ToolError('unknown or recursive desktop tool', code='bad_args')
        args.setdefault('observation_mode', 'compact')
        result = execute(tool, args)
        if isinstance(result, dict) and result.get("all_ok") is False:
            raise ToolError("desktop batch stopped; inspect its partial effects",
                            code="verification_failed", action_status="partial")
        return result

    def screenshot(self, **args):
        return self.call('screenshot', **args)

    def click(self, x, y, *, target, **args):
        return self.call('pointer_click', x=x, y=y, expect_window=target, **args)

    def move(self, x, y, **args):
        return self.call('pointer_move', x=x, y=y, **args)

    def path(self, points, *, target, **args):
        return self.call('pointer_path', points=points, target=target, **args)

    def type(self, text, *, target, **args):
        return self.call('type_text', text=text, target=target, **args)

    def key(self, combo, *, target, **args):
        return self.call('press_keys', combo=combo, target=target, **args)

    def wait(self, condition, **args):
        result = self.call('wait_for', condition=condition, **args)
        if not result.get('met'):
            raise ToolError('required wait condition was not met', code='timeout',
                            action_status='not_started')
        return result

    sleep = staticmethod(pause)


def tool_desktop_exec(args):
    code = args.get('code')
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 65536:
        raise ToolError('code must be nonempty Python, at most 64 KiB', code='bad_args')
    if args.get('reset'):
        GLOBALS.clear()
        from .observations import FRAMES
        FRAMES.clear()
    if not GLOBALS:
        GLOBALS.update(desktop=Desktop(), log=log, display=display)
    OUTPUT.clear()
    IMAGES.clear()
    # stdout is tool output, never transport. A bounded writer also covers print loops.
    class Writer(io.TextIOBase):
        def write(self, value):
            log(value)
            return len(value)
    error = None
    try:
        compiled = compile(code, '<desktop_exec>', 'exec')
        with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
            exec(compiled, GLOBALS)
    except Exception as e:
        error = {'code': getattr(e, 'code', 'python_error'),
                 'message': str(e)[:2000], 'type': type(e).__name__}
    result = {'output': OUTPUT.copy(), 'state_preserved': True,
              'all_ok': error is None, '__inline_image__s': IMAGES.copy()}
    if error:
        result['error'] = error
        result['code'] = error['code']
        result['action_status'] = 'unknown'
        result['recovery'] = 'Variables and earlier actions remain; inspect before retrying.'
    return result
