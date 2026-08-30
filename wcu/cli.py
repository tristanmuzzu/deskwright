"""`wayland-computer-use` -- the console-script entry point for the MCP server.

Installing this package puts this on PATH, so an MCP client is registered
against a command rather than a path inside a checkout:

    claude mcp add wayland-computer-use --scope user -- wayland-computer-use

`mcp_server.py` in a checkout is the same entry point by another name, kept
because existing registrations point at that exact path.

    wayland-computer-use                    # speak MCP on stdio
    wayland-computer-use --self-test        # prove every capability, exit
    wayland-computer-use --session headless # drive the private virtual desktop
"""
from __future__ import annotations

import sys

from .session import resolve_session, start_deferred


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    resolve_session(as_server=True, argv=argv)
    from .server import self_test, serve
    if "--self-test" in argv:
        # The self-test calls tool functions in-process, not through serve(),
        # so a deferred headless start must happen before it, not lazily --
        # and it must start the session that was actually named.
        start_deferred()
        return self_test()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
