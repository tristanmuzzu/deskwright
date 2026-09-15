#!/usr/bin/env python3
"""The a11y registry on a headless session: is one actually running?

THE BUG (found 2026-09-15, Ubuntu 26.04 / GNOME Shell 50.1 / at-spi2-core
2.60.4): a headless session comes up and its extension answers, but
org.a11y.atspi.Registry is owned by nobody on the headless a11y bus, so
ui_apps says "0 apps", ui_tree and ui_find fail with "no application named
'gnome-shell' on the AT-SPI bus", and the headless self-test fails 2/18.

The chain, from the session's own log:

  1. The headless session runs a PRIVATE dbus-daemon, which has no systemd
     behind it. That is the point of it (nothing can reach the user's
     desktop), but it also means nothing can be activated through systemd.
  2. The session's at-spi-bus-launcher starts the a11y broker fine, then
     tries to start at-spi2-registryd by asking the broker to activate
     org.freedesktop.systemd1 -- and on a bus with no systemd that name is
     served by the stub systemd ships for systemd-less buses,
     Exec=/bin/false, which exits 1. The dbus-daemon log says exactly this,
     once per retry:
         Activating service name='org.freedesktop.systemd1' ... failed:
         Process org.freedesktop.systemd1 exited with status 1
  3. So the registry never starts, and every ui_* tool on the headless
     session fails.

The user's real session never sees this: its bus has the real systemd
behind org.freedesktop.systemd1, activation works, the registry runs.

This script proves the bug without guessing, by asking the headless
session's a11y bus itself. It needs a live GNOME Wayland login to create
the headless session (a second gnome-shell compositing a virtual monitor),
and it cleans up after itself unless --keep is passed.

Exit code 0 = registry is up on the headless session. Anything else = the
bug is present (printout says which step failed). On a checkout of main
this fails; with headless.py spawning registryd itself it passes.

    ./tests/test_headless_atspi.py
    ./tests/test_headless_atspi.py --name repro    # a session of your own
    ./tests/test_headless_atspi.py --keep          # leave the session up
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from deskwright import headless

passed = failed = 0

def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {label:44} {detail}")
    else:
        failed += 1
        print(f"FAIL  {label:44} {detail}")

# The two probes below are deliberately inline gdbus calls rather than
# helpers from headless.py: this script has to run UNCHANGED on a checkout
# of main, where the bug lives, so it cannot depend on any fix-branch code.


def a11y_socket(session_bus: str) -> str | None:
    """Resolve the session's a11y bus socket the way every AT-SPI client
    does: ask org.a11y.Bus on the session bus."""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.a11y.Bus",
             "--object-path", "/org/a11y/bus", "--method",
             "org.a11y.Bus.GetAddress"],
            env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=session_bus),
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        # ('unix:path=/run/user/1000/deskwright-headless/at-spi/bus',)
        first = out.stdout.strip().strip("()").split(",")[0].strip().strip("'\"")
        return first[len("unix:path="):] if first.startswith("unix:path=") else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def registry_owned(a11y_bus: str) -> bool:
    """Does org.a11y.atspi.Registry have an owner on that a11y bus?"""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--address", f"unix:path={a11y_bus}",
             "--dest", "org.freedesktop.DBus", "--object-path",
             "/org/freedesktop/DBus", "--method",
             "org.freedesktop.DBus.GetNameOwner", "org.a11y.atspi.Registry"],
            capture_output=True, timeout=10)
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def main() -> int:
    keep = "--keep" in sys.argv
    args = sys.argv[1:]
    name = None
    if "--name" in args:
        name = args[args.index("--name") + 1]

    try:
        report = headless.ensure(name=name)
    except Exception as e:
        print(f"could not start a headless session: {type(e).__name__}: {e}")
        return 2
    if not report.get("running"):
        print("headless session did not come up:")
        print(json.dumps(report, indent=1))
        return 2

    address = report["bus_address"]
    print(f"headless session {report['name']!r} running, "
          f"shell pid {report.get('shell_pid')}")
    print(f"log: {report.get('log')}")

    # Pin THIS process to the session before touching AT-SPI. ensure()
    # deliberately does not pin (the server's entry point does it at
    # startup, via deskwright/session.py), and an unpinned process reads
    # the USER's a11y bus -- where the registry runs and everything looks
    # fine. Forgetting the pin is exactly how this bug hides from a casual
    # check: the broken bus is only visible from inside the session.
    headless.pin_env(report, os.environ)

    # 1. The a11y bus itself must exist: its launcher does not depend on
    #    systemd, so this passes even with the bug present.
    socket_path = a11y_socket(address)
    check("a11y bus socket exists",
          socket_path is not None and Path(socket_path).exists(),
          socket_path or "org.a11y.Bus.GetAddress failed")

    # 2. The registry name must be owned ON THE A11Y BUS. This is the
    #    check that fails on main: the socket answers, nobody owns the
    #    registry name, every ui_* tool fails downstream.
    alive = registry_owned(socket_path) if socket_path else False
    check("org.a11y.atspi.Registry is owned", alive,
          "" if alive else "socket answers but the registry name has no owner")

    # 3. And the tools must see it: ui_apps through the real tool surface.
    #    A registry that answers but exposes no apps would be its own bug.
    #    Retried for up to 10s: the shell's atk-bridge finishes its
    #    handshake a moment after the registry owns the name (measured:
    #    0 apps immediately after start, apps present seconds later).
    apps: dict = {}
    deadline = time.monotonic() + 10.0
    while True:
        try:
            from deskwright.atspi import tool_ui_apps
            apps = tool_ui_apps({})
            if apps["count"] >= 1 or time.monotonic() >= deadline:
                break
        except Exception as e:
            if time.monotonic() >= deadline:
                apps = {"count": -1, "error": f"{type(e).__name__}: {e}"}
                break
        time.sleep(0.5)
    check("ui_apps sees apps through it", apps.get("count", 0) >= 1,
          f"{apps.get('count')} apps: "
          f"{', '.join(a['name'] for a in apps.get('apps', [])[:4])}")

    if not keep:
        stopped = headless.stop(name=report["name"])
        print(f"cleanup: {'ended' if stopped.get('stopped') else 'nothing to end'}")

    print(f"\n{passed}/{passed + failed} checks passed")
    if failed:
        print("\nThe headless session's AT-SPI registry is not running. This is "
              "the bug: the private bus cannot activate registryd through "
              "systemd, so ui_* tools have nothing to talk to.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
