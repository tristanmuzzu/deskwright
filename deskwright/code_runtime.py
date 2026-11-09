"""Opt-in persistent Python with explicit observations over guarded desktop tools."""
from __future__ import annotations

import builtins
import contextlib
import difflib
import io
import json
import time

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


def _unknown_tool(tool):
    from .server import TOOL_SCHEMAS
    names = [s['name'] for s in TOOL_SCHEMAS if s['name'] != 'desktop_exec']
    close = difflib.get_close_matches(str(tool), names, n=3, cutoff=0.4)
    hint = f" Did you mean {', '.join(close)}?" if close else ''
    return (f"unknown or recursive desktop tool {tool!r}.{hint} "
            f"desktop.describe() lists all {len(names)} tools.")


def _helpers(desktop):
    """The helpers the tool description names, as plain globals.

    The description has always said click(), type(), key(), wait() and
    sleep() are available; until 2026-11-09 only desktop.click() and the
    rest existed, and five sessions hit NameError on the first try.
    `type` keeps working as the builtin: type(obj) and type(name, bases,
    dict) still do what Python means, only type(text, target=...) types.
    """
    def type_(*a, **kw):
        if 'target' in kw:
            return desktop.type(*a, **kw)
        return builtins.type(*a, **kw)
    names = ('click', 'move', 'scroll', 'path', 'key', 'wait', 'sleep', 'observe',
             'query', 'screenshot', 'describe', 'call', 'wait_process', 'deadline_left')
    out = {n: getattr(desktop, n) for n in names}
    out['type'] = type_
    return out


class Desktop:
    def __init__(self):
        self.last_call = None
        self.effects = False

    def describe(self, tool=None):
        """The live tool contract, without a desktop round trip.

        describe() with no argument lists the tool names: agents called it
        with guesses like 'screen_size', 'scroll' and 'mouse_click' and got
        only "unknown tool" back (2026-10-30, 17 describe calls in one run).
        """
        from .server import TOOL_SCHEMAS
        if tool is None:
            return sorted(s['name'] for s in TOOL_SCHEMAS if s['name'] != 'desktop_exec')
        match = next((s for s in TOOL_SCHEMAS if s['name'] == tool), None)
        if match is None or tool == 'desktop_exec':
            raise ToolError(_unknown_tool(tool), code='bad_args')
        return match['inputSchema']

    def call(self, tool, **args):
        from .server import _READ_ONLY_TOOLS, HANDLERS, validate_tool_args
        if tool not in HANDLERS or tool == 'desktop_exec':
            raise ToolError(_unknown_tool(tool), code='bad_args')
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

    def click(self, x, y, *, target=None, **args):
        """Click; with target, nothing is clicked unless that window is there."""
        if target is not None:
            args['expect_window'] = target
        return self.call('pointer_click', x=x, y=y, **args)

    def scroll(self, x, y, dy=0, dx=0, **args):
        """dy/dx are wheel clicks, positive down/right."""
        return self.call('pointer_scroll', x=x, y=y, dy=dy, dx=dx, **args)

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

    @staticmethod
    def deadline_left():
        """Seconds before this call's deadline; plan long work in chunks."""
        return round(remaining(60), 2)

    @staticmethod
    def wait_process(proc, timeout=None, tail=None, lines=20):
        """Block until a process exits or the time runs out, instead of polling.

        An overnight run on 2026-10-30 spent about 237 desktop_exec calls on
        nothing but proc.poll() and reading a log tail. This waits inside one
        call, up to `timeout` or the call's deadline less a 2 s margin, reaps
        the child if it is ours, and returns the last `lines` of `tail`.
        """
        import os
        import subprocess
        budget = remaining(60) - 2
        if timeout is not None:
            budget = min(budget, float(timeout))
        start = time.monotonic()
        returncode = None
        if isinstance(proc, subprocess.Popen):
            try:
                returncode = proc.wait(timeout=max(0.0, budget))
            except subprocess.TimeoutExpired:
                returncode = None
            pid = proc.pid
        else:
            pid = int(proc)
            while time.monotonic() - start < budget:
                try:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        returncode = os.waitstatus_to_exitcode(status)
                        break
                except ChildProcessError:
                    # Not our child: all we can learn is whether it still exists.
                    if not os.path.exists(f'/proc/{pid}'):
                        returncode = 'exited (not our child; code unknown)'
                        break
                pause(0.1)
        out = {'pid': pid, 'exited': returncode is not None, 'returncode': returncode,
               'waited_s': round(time.monotonic() - start, 2)}
        if tail:
            try:
                with open(tail, errors='replace') as f:
                    out['tail'] = f.read().splitlines()[-int(lines):]
            except OSError as e:
                out['tail_error'] = str(e)
        return out

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
        GLOBALS.update(_helpers(GLOBALS['desktop']))
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
