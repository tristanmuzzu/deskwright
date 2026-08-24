"""A second GNOME session on a virtual monitor, for unattended runs.

WHY THIS EXISTS

An agent that needs the user's screen is only half autonomous. This module
starts a *separate* GNOME session -- its own session bus, its own
`gnome-shell --headless` compositing a virtual monitor -- and a server
process pinned to it drives that session with the identical tool surface,
while the user keeps the physical screen. Proven end to end in the
2026-08-23 spike (docs/spikes.md): the headless shell loads the bundled
extension unmodified, apps launch onto the virtual monitor, screenshots
show a full desktop the user never sees.

The whole trick is that nothing in wcu/ assumes the primary session.
Every mechanism -- extension D-Bus (gdbus subprocesses), AT-SPI, mutter
RemoteDesktop/ScreenCast (`Gio.bus_get_sync`), `gio launch` -- resolves
the session from `DBUS_SESSION_BUS_ADDRESS` and `WAYLAND_DISPLAY` at call
time. So "drive the headless session" is exactly `pin_env()` before the
first tool call, and no tool code changes at all.

The one deliberate exception is ydotool: it writes into /dev/uinput BELOW
the compositor, so its events land on the machine's real seat -- the
user's screen -- no matter what this process's environment says. input.py
refuses it when `WCU_HEADLESS` is set (code `wrong_session`).

Cost, measured in the spike: ~293 MB RSS for the headless gnome-shell.
Affordable for a bounded background task; not something to leave idle,
which is why `stop` exists and `status` reports RSS.

State lives in $XDG_STATE_HOME/wayland-computer-use/headless.json --
pids, bus address, display name -- so any later process (the CLI, a
server ensure()) can find, verify or end the session. Liveness is never
trusted from the file alone: pids are checked against /proc/<pid>/comm
(pid reuse) and the bus is pinged before either is believed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

from .errors import ToolError
from .shell import NEW_BUS, NEW_PATH

DEFAULT_SIZE = "1280x720"
DEFAULT_DISPLAY = "wayland-wcu"

# The spike's shell reached "extension answering" well inside 15 s on this
# machine; the margin covers a loaded box without making a real failure slow
# to report.
START_TIMEOUT_S = 45.0
STOP_TIMEOUT_S = 10.0


def _state_dir() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(state, "wayland-computer-use")
    os.makedirs(d, exist_ok=True)
    return d


def _state_file() -> str:
    return os.path.join(_state_dir(), "headless.json")


def _read_state() -> dict[str, Any] | None:
    try:
        with open(_state_file()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _pid_is(pid: int, comm: str) -> bool:
    """Alive AND still the process we started -- /proc/<pid>/comm guards
    against pid reuse deciding to SIGTERM an innocent bystander later."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() == comm
    except OSError:
        return False


def _bus_alive(address: str, timeout: float = 5.0) -> bool:
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address)
    try:
        proc = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.GetId"],
            env=env, capture_output=True, timeout=timeout)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _extension_answering(address: str) -> bool:
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address)
    try:
        xml = subprocess.run(
            ["gdbus", "introspect", "--session", "--dest", NEW_BUS,
             "--object-path", NEW_PATH, "--xml"],
            env=env, capture_output=True, text=True, timeout=10).stdout
        return "<method" in xml
    except (subprocess.TimeoutExpired, OSError):
        return False


def _rss_mb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def status() -> dict[str, Any]:
    """What is true right now, never what the state file wishes were true."""
    state = _read_state()
    if not state:
        return {"running": False, "detail": "no headless session recorded"}
    shell_ok = _pid_is(int(state.get("shell_pid", -1)), "gnome-shell")
    dbus_ok = _pid_is(int(state.get("dbus_pid", -1)), "dbus-daemon")
    bus_ok = shell_ok and _bus_alive(state["bus_address"])
    ext_ok = bus_ok and _extension_answering(state["bus_address"])
    running = shell_ok and dbus_ok and bus_ok
    report: dict[str, Any] = {
        "running": running,
        "shell_process": shell_ok, "dbus_process": dbus_ok,
        "bus_reachable": bus_ok, "extension_answering": ext_ok,
        **{k: state[k] for k in ("bus_address", "wayland_display", "size",
                                 "runtime_dir", "shell_pid", "dbus_pid",
                                 "started_at", "log")
           if k in state},
    }
    if running:
        rss = _rss_mb(int(state["shell_pid"]))
        if rss is not None:
            report["shell_rss_mb"] = rss
    else:
        report["detail"] = ("stale state file -- the session is gone; "
                            "`wcu-headless stop` will clean it up, "
                            "`wcu-headless start` will replace it")
    return report


def start(size: str = DEFAULT_SIZE, display: str = DEFAULT_DISPLAY) -> dict[str, Any]:
    """Bring up the private bus + headless shell, wait until the extension
    answers on it, record the result. Idempotent by refusal: a live session
    is returned as-is, never doubled (two shells would fight over RAM and
    the display name)."""
    current = status()
    if current["running"]:
        current["detail"] = "already running -- reusing"
        return current
    if _read_state():
        _cleanup_state()          # stale file from a dead session

    log_path = os.path.join(_state_dir(), "headless-shell.log")
    log = open(log_path, "ab")

    # A PRIVATE runtime dir, because two sessions sharing one is how the
    # headless session broke the user's desktop (2026-08-24): both sessions'
    # at-spi-bus-launchers bind $XDG_RUNTIME_DIR/at-spi/bus, last bind wins
    # the path, and every primary-session app then times out registering
    # against the wrong session's a11y bus. Session-scoped sockets (a11y,
    # the wayland display itself) live here; the per-USER pipewire daemon is
    # shared back in via symlink -- mutter's ScreenCast streams (which
    # absolute pointer input rides on) need it.
    runtime_dir = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
        "wcu-headless")
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    real_runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for sock in ("pipewire-0", "pipewire-0-manager"):
        target = os.path.join(real_runtime, sock)
        link = os.path.join(runtime_dir, sock)
        if os.path.exists(target) and not os.path.exists(link):
            os.symlink(target, link)

    # The daemon's environment is what D-Bus ACTIVATED apps inherit
    # (`gio launch` of a DBusActivatable app asks the daemon to spawn it).
    # Left alone it holds the USER'S WAYLAND_DISPLAY, and the first launched
    # app opens on the real screen -- exactly the thing this module exists
    # to prevent (observed 2026-08-24). So the daemon is born already
    # pointing at the display the shell will create.
    daemon_env = os.environ.copy()
    daemon_env["WAYLAND_DISPLAY"] = display
    daemon_env["XDG_RUNTIME_DIR"] = runtime_dir
    daemon_env.pop("DISPLAY", None)
    dbus = subprocess.Popen(
        ["dbus-daemon", "--session", "--print-address=1", "--nofork"],
        env=daemon_env, stdout=subprocess.PIPE, stderr=log,
        start_new_session=True)
    assert dbus.stdout is not None
    address = dbus.stdout.readline().decode().strip()
    if not address:
        dbus.terminate()
        raise ToolError("dbus-daemon produced no address -- it exited "
                        f"immediately; see {log_path}", code="capture_failed")

    env = os.environ.copy()
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    env["XDG_RUNTIME_DIR"] = runtime_dir
    # The headless shell CREATES a display; it must not attach to the user's.
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    shell = subprocess.Popen(
        ["gnome-shell", "--headless", "--wayland-display", display,
         "--virtual-monitor", size],
        env=env, stdout=log, stderr=log, start_new_session=True)

    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if shell.poll() is not None:
            dbus.terminate()
            raise ToolError(
                f"gnome-shell --headless exited rc={shell.returncode} before "
                f"the extension came up; see {log_path}", code="capture_failed")
        if _extension_answering(address):
            break
        time.sleep(0.5)
    else:
        shell.terminate()
        dbus.terminate()
        raise ToolError(
            f"headless shell started but {NEW_BUS} never answered within "
            f"{START_TIMEOUT_S:.0f}s; see {log_path}", code="extension_unavailable")

    # Belt and braces on top of daemon_env: pin the activation environment
    # explicitly, so even a daemon that scrubs its inherited environment
    # spawns apps onto the headless display and the private bus.
    subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
         "--object-path", "/org/freedesktop/DBus",
         "--method", "org.freedesktop.DBus.UpdateActivationEnvironment",
         json.dumps({"WAYLAND_DISPLAY": display,
                     "XDG_RUNTIME_DIR": runtime_dir,
                     "DBUS_SESSION_BUS_ADDRESS": address})],
        env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address),
        capture_output=True, timeout=10)

    state = {
        "bus_address": address, "wayland_display": display, "size": size,
        "runtime_dir": runtime_dir,
        "shell_pid": shell.pid, "dbus_pid": dbus.pid,
        "started_at": time.time(), "log": log_path,
    }
    with open(_state_file(), "w") as f:
        json.dump(state, f, indent=1)
    out = status()
    out["detail"] = "started"
    return out


def stop() -> dict[str, Any]:
    """End the session and remove the record. Termination order matters:
    the shell first (it is the bus client), the daemon second."""
    state = _read_state()
    if not state:
        return {"stopped": False, "detail": "no headless session recorded"}
    ended = []
    for key, comm in (("shell_pid", "gnome-shell"), ("dbus_pid", "dbus-daemon")):
        pid = int(state.get(key, -1))
        if not _pid_is(pid, comm):
            continue
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline and _pid_is(pid, comm):
            time.sleep(0.2)
        if _pid_is(pid, comm):
            os.kill(pid, signal.SIGKILL)   # a compositor with no user session
            time.sleep(0.5)                # to save may be shot safely
        ended.append(comm)
    _cleanup_state()
    return {"stopped": True, "ended": ended,
            "detail": "headless session ended" if ended
            else "session was already gone; record removed"}


def _cleanup_state() -> None:
    try:
        os.unlink(_state_file())
    except OSError:
        pass


def ensure(size: str = DEFAULT_SIZE, display: str = DEFAULT_DISPLAY) -> dict[str, Any]:
    """Running session, starting one if needed. What a pinned server calls
    at startup so `WCU_SESSION=headless` is self-sufficient."""
    current = status()
    if current["running"]:
        return current
    return start(size=size, display=display)


def pin_env(state: dict[str, Any], env: Any = os.environ) -> None:
    """Point THIS process at the headless session. Must run before the first
    tool call; every backend resolves the session lazily from these three
    variables, so nothing else needs to change."""
    env["DBUS_SESSION_BUS_ADDRESS"] = state["bus_address"]
    env["WAYLAND_DISPLAY"] = state["wayland_display"]
    if state.get("runtime_dir"):
        env["XDG_RUNTIME_DIR"] = state["runtime_dir"]
    # The flag input.py checks to refuse ydotool -- uinput injection lands on
    # the real seat regardless of this environment.
    env["WCU_HEADLESS"] = "1"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    size, display = DEFAULT_SIZE, DEFAULT_DISPLAY
    if "--size" in args:
        size = args[args.index("--size") + 1]
    if "--display" in args:
        display = args[args.index("--display") + 1]
    try:
        if cmd == "start":
            report = start(size=size, display=display)
        elif cmd == "stop":
            report = stop()
        elif cmd == "status":
            report = status()
        elif cmd == "env":
            state = _read_state()
            if not state or not status()["running"]:
                print("# no running headless session -- `wcu-headless start` first",
                      file=sys.stderr)
                return 1
            print(f"export DBUS_SESSION_BUS_ADDRESS='{state['bus_address']}'")
            print(f"export WAYLAND_DISPLAY='{state['wayland_display']}'")
            if state.get("runtime_dir"):
                print(f"export XDG_RUNTIME_DIR='{state['runtime_dir']}'")
            print("export WCU_HEADLESS=1")
            return 0
        else:
            print("usage: wcu-headless [start|stop|status|env] "
                  f"[--size {DEFAULT_SIZE}] [--display {DEFAULT_DISPLAY}]",
                  file=sys.stderr)
            return 2
    except ToolError as e:
        print(e.wire_text(), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1))
    if cmd == "start" and report.get("running"):
        print("\n# pin a server to it:  WCU_SESSION=headless python3 mcp_server.py",
              file=sys.stderr)
    return 0 if report.get("running") or report.get("stopped") or cmd == "status" else 1


if __name__ == "__main__":
    sys.exit(main())
