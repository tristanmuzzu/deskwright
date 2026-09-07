"""One execution contract for MCP batches and the optional Python adapter.

The supervising process owns hard cancellation. This context owns cooperative
deadlines, per-primitive halt checks, partial-outcome records and a desktop lease.
No claim is made that Python execution itself is an OS sandbox.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ToolError


def clock() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else time.monotonic()


@dataclass
class Execution:
    deadline: float
    trace: list = field(default_factory=list)
    progress: object = None


CURRENT = contextvars.ContextVar("desktop_execution", default=None)


def check() -> None:
    ctx = CURRENT.get()
    if ctx and clock() >= ctx.deadline:
        raise ToolError("execution deadline reached; inspect any partial input", code="timeout")


def remaining(default: float) -> float:
    check()
    ctx = CURRENT.get()
    return max(.001, min(default, ctx.deadline - clock())) if ctx else default


def pause(seconds: float) -> None:
    if CURRENT.get() is None:
        time.sleep(seconds)
        return
    end = clock() + seconds
    while clock() < end:
        check()
        time.sleep(max(0, min(.05, end - clock())))


def session_key() -> str:
    # The bus identifies the actual desktop even if callers use different aliases.
    return hashlib.sha256(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "unbound").encode()).hexdigest()[:24]


@contextlib.contextmanager
def operation(seconds: float = 60, progress=None):
    if CURRENT.get():
        yield CURRENT.get()
        return
    ctx = Execution(clock() + seconds, progress=progress)
    token = CURRENT.set(ctx)
    root = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "deskwright-leases"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with (root / session_key()).open("a") as lease:
            while True:
                check()
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pause(.05)
            yield ctx
    finally:
        CURRENT.reset(token)


def execute(name: str, args: dict, *, handler=None):
    from . import server, shell
    from .observations import map_input
    check()
    ctx = CURRENT.get()
    acting = name not in server._READ_ONLY_TOOLS
    if ctx and acting and shell.halt_active():
        raise ToolError("human halt switch engaged; no further input", code="halted",
                        action_status="not_started")
    args = map_input(name, dict(args))
    if args.pop("observation_mode", None) == "compact":
        args["look"] = False
    record = {"tool": name, "action_status": "started"}
    if ctx:
        if len(ctx.trace) >= 4096:
            raise ToolError("execution exceeds 4096 primitives; inspect progress before continuing", code="bad_args")
        ctx.trace.append(record)
        if ctx.progress:
            ctx.progress(record)
    try:
        result = (handler or server.HANDLERS[name])(args)
    except ToolError as e:
        record.update(action_status=e.action_status, code=e.code)
        if ctx and acting:
            from .journal import record as journal_record
            journal_record(name, args, {"error": str(e), "code": e.code, "action_status": e.action_status})
        if ctx and ctx.progress:
            ctx.progress(record)
        raise
    except Exception as e:
        record.update(action_status="unknown")
        if ctx and acting:
            from .journal import record as journal_record
            journal_record(name, args, {"error": str(e), "code": "crash", "action_status": "unknown"})
        if ctx and ctx.progress:
            ctx.progress(record)
        raise
    record["action_status"] = "applied" if acting else "observed"
    if isinstance(result, dict):
        if result.get("all_ok") is False:
            record["action_status"] = "partial"
        result.setdefault("action_status", record["action_status"])
        record["action_status"] = result["action_status"]
    if ctx and acting:
        from .journal import record as journal_record
        journal_record(name, args, result)
    if ctx and ctx.progress:
        ctx.progress(record)
    return result
