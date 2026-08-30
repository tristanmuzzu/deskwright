"""Pin this process to a headless session when asked.

`WCU_SESSION=headless` (or `--session headless`) makes the server drive a
private virtual-monitor session instead of the user's desktop -- starting it
first if needed (wcu/headless.py). Every backend resolves the session lazily
from `DBUS_SESSION_BUS_ADDRESS`/`WAYLAND_DISPLAY`, so pinning is purely an
environment operation and no tool code knows the difference.

`headless:<name>` picks WHICH headless desktop. Two agent sessions that want
to work at the same time without watching each other's windows move register
servers with different names; the same name means the same desktop, which is
what everybody shared before names existed.
"""
from __future__ import annotations

import os
import sys


def resolve_session(as_server: bool, argv: list[str] | None = None) -> None:
    """Set the environment that pins this process to the requested session.

    `as_server` distinguishes the two callers. Running AS the server, a cold
    session start costs 15-20s, and paying it here means paying it inside the
    MCP client's initialize window -- measured 2026-08-25 killing the
    connection before the first call. So initialize stays instant: pin now if
    the session is already up, otherwise leave a marker and let the FIRST TOOL
    CALL start it (same total wait, no timeout window; wcu/server.py owns the
    marker). A test or script that will call tool functions directly, bypassing
    serve(), passes False and gets the session ready before it returns.

    With `WCU_SESSION` unset this is a no-op.
    """
    argv = sys.argv if argv is None else argv
    session = os.environ.get("WCU_SESSION", "")
    if "--session" in argv:
        session = argv[argv.index("--session") + 1]
    if not session or session == "primary":
        return
    kind, _, raw_name = session.partition(":")
    if kind != "headless":
        print(f"unknown --session {session!r}; use 'primary', 'headless', or "
              "'headless:<name>' for a specific virtual desktop", file=sys.stderr)
        sys.exit(2)
    from .errors import ToolError
    from .headless import ensure, pin_env, session_name, status
    try:
        name = session_name(raw_name or None)
    except ToolError as e:
        print(e.wire_text(), file=sys.stderr)
        sys.exit(2)
    # The lazy path below re-reads this instead of closing over `name`, so the
    # first tool call starts the session the flag asked for.
    os.environ["WCU_SESSION"] = f"headless:{name}"
    if as_server:
        st = status(name)
        if st.get("running"):
            pin_env(st)
        else:
            os.environ["WCU_HEADLESS_LAZY"] = "1"
        return
    pin_env(ensure(name=name))
