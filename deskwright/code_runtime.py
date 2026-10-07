"""Opt-in persistent Python with explicit observations over guarded desktop tools."""
from __future__ import annotations

import contextlib
import io
import json

from .errors import ToolError
from .execution import check, execute, pause, remaining

GLOBALS = {}
OUTPUT = []
IMAGES = []


def log(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if sum(map(len, OUTPUT)) + len(text) > 32_000:
        raise ToolError('code output exceeds 32000 characters; return a concise summary', code='bad_args')
    OUTPUT.append(text)


def display(observation):
    _attach_image(observation)
    from .capture import _INLINE_KEY
    log({k: v for k, v in observation.items() if k != _INLINE_KEY})


def _attach_image(observation):
    # Only captured tool images: display is not an image renderer or file loader.
    from .capture import _INLINE_KEY
    if not isinstance(observation, dict) or _INLINE_KEY not in observation:
        raise ToolError('display expects desktop.screenshot() result', code='bad_args')
    if len(IMAGES) >= 4:
        raise ToolError('at most four displayed observations per call', code='bad_args')
    IMAGES.append(observation[_INLINE_KEY])


class Desktop:
    def __init__(self):
        self.last_call = None
        self.effects = False

    def describe(self, tool):
        """The live tool contract, without a desktop round trip."""
        from .server import TOOL_SCHEMAS
        match = next((s for s in TOOL_SCHEMAS if s['name'] == tool), None)
        if match is None or tool == 'desktop_exec':
            raise ToolError('unknown or recursive desktop tool', code='bad_args')
        return match['inputSchema']

    def call(self, tool, **args):
        from .server import _READ_ONLY_TOOLS, HANDLERS, validate_tool_args
        if tool not in HANDLERS or tool == 'desktop_exec':
            raise ToolError('unknown or recursive desktop tool', code='bad_args')
        self.last_call = (tool, dict(args))
        validate_tool_args(tool, args)
        args.setdefault('observation_mode', 'compact')
        try:
            result = execute(tool, args)
        except ToolError as exc:
            if tool not in _READ_ONLY_TOOLS and exc.action_status != 'not_started':
                self.effects = True
            raise
        if tool not in _READ_ONLY_TOOLS:
            self.effects = True
        if isinstance(result, dict) and result.get("all_ok") is False:
            raise ToolError("desktop batch stopped; inspect its partial effects",
                            code="verification_failed", action_status="partial")
        return result

    def screenshot(self, **args):
        return self.call('screenshot', **args)

    def observe(self, window_id, **args):
        """Discover native controls or display a bounded visual fallback."""
        from .capture import _INLINE_KEY
        result = self.call('ui_observe', window_id=window_id, **args)
        if _INLINE_KEY in result:
            _attach_image(result)
            result = {k: v for k, v in result.items() if k != _INLINE_KEY}
        return result

    def query(self, window_id, *, timeout_s=5, within=None, **controls):
        """Exact named selectors; stop the batch unless all predicates hold."""
        return self.call('ui_query', window_id=window_id, controls=controls,
                         **({'within': within} if within is not None else {}),
                         timeout_s=timeout_s)['controls']

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
                            action_status='not_started', details=result)
        return result

    sleep = staticmethod(pause)

    def recovery(self):
        """Attach a fresh visual observation after a stopped batch, never input."""
        from . import semantic, shell
        check()
        if remaining(2) < 1 or shell.halt_active():
            return {'observation_skipped': 'deadline or human halt'}
        if len(IMAGES) >= 4:
            return {'observation_skipped': 'image capacity reached'}
        windows = shell.list_windows()
        _tool, args = self.last_call or ('', {})
        target = args.get('window_id', args.get('target', args.get('expect_window')))
        if 'ref' in args and args['ref'] in semantic.REFS:
            target = semantic.REFS[args['ref']].window['id']
        matches = [w for w in windows if w['id'] == target]
        # A modal or a changed app may own the screen; show the focused window
        # rather than pretending that the previous target is still interactive.
        focused = next((w for w in windows if w.get('focused')), None)
        win = focused or (matches[0] if matches else None)
        shot = self.screenshot(**({'window': win['id']} if win else {}))
        _attach_image(shot)
        from .capture import _INLINE_KEY
        return {'window': win, 'windows': [{k: w[k] for k in ('id', 'pid', 'title', 'wm_class')}
                                         for w in windows[:20]],
                'screenshot': {k: v for k, v in shot.items() if k != _INLINE_KEY},
                'input_replayed': False}


def tool_desktop_exec(args):
    code = args.get('code')
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 65536:
        raise ToolError('code must be nonempty Python, at most 64 KiB', code='bad_args')
    if args.get('reset'):
        GLOBALS.clear()
        from .observations import FRAMES
        FRAMES.clear()
        from .semantic import REFS
        REFS.clear()
        from .hybrid import ROUTES
        ROUTES.clear()
    if not GLOBALS:
        GLOBALS.update(desktop=Desktop(), log=log, display=display)
    OUTPUT.clear()
    IMAGES.clear()
    desktop = GLOBALS['desktop']
    desktop.last_call, desktop.effects = None, False
    # stdout is tool output, never transport. A bounded writer also covers print loops.
    class Writer(io.TextIOBase):
        def write(self, value):
            log(value)
            return len(value)
    error = None
    recovery = None
    try:
        compiled = compile(code, '<desktop_exec>', 'exec')
        with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
            exec(compiled, GLOBALS)
    except Exception as e:
        error = {'code': getattr(e, 'code', 'python_error'),
                 'message': str(e)[:2000], 'type': type(e).__name__,
                 'action_status': getattr(e, 'action_status', 'unknown')}
        if getattr(e, 'details', None):
            error['details'] = e.details
        if isinstance(e, ToolError) and e.code in {
            'timeout', 'ambiguous_control', 'widget_missing', 'window_not_found', 'stale_observation',
            'focus_not_acquired', 'occluded', 'atspi_write_failed', 'verification_failed'}:
            try:
                recovery = desktop.recovery()
            except Exception as recovery_error:
                recovery = {'observation_unavailable': type(recovery_error).__name__}
    result = {'output': OUTPUT.copy(), 'state_preserved': True,
              'all_ok': error is None, '__inline_image__s': IMAGES.copy()}
    if error:
        result['error'] = error
        result['code'] = error['code']
        result['action_status'] = 'partial' if desktop.effects else error['action_status']
        result['recovery'] = 'Variables and earlier actions remain; inspect before retrying.'
        if recovery:
            result['recovery_observation'] = recovery
    return result
