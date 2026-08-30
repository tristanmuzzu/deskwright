"""Named GNOME sessions on virtual monitors, for unattended runs.

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

MORE THAN ONE (2026-08-27)

Sessions are NAMED. Two agent sessions that both want a desktop of their
own ask for different names and get genuinely separate compositors; two
that pass the same name share one, which is what `WCU_SESSION=headless`
(name "default") did for everybody before names existed. Everything that
must not collide is derived from the name:

    state file    $XDG_STATE_HOME/wayland-computer-use/headless-<name>.json
    runtime dir   $XDG_RUNTIME_DIR/wcu-headless[-<name>]
    display       wayland-wcu[-<name>]

The runtime dir is the one that actually bites: two sessions sharing one
fight over `at-spi/bus`, last bind wins the path, and the OTHER session's
apps then time out registering accessibility (found the hard way
2026-08-24 -- against the user's real desktop). Suffixing it per name is
the same fix applied a second time.

`default` keeps the unsuffixed paths, so a session started before this
change is still found, still driven and still stoppable by the new code.

Cost, measured in the spike: ~293 MB RSS for the headless gnome-shell,
per session. That is why `MAX_SESSIONS` and the free-memory check exist:
the failure mode of one too many is a machine that swaps, and an agent
cannot see that happening.

Liveness is never trusted from a state file: pids are checked against
/proc/<pid>/comm (pid reuse) and the bus is pinged before either is
believed.
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any

from .errors import ToolError
from .shell import NEW_BUS, NEW_PATH

DEFAULT_SIZE = "1280x720"
DEFAULT_NAME = "default"
DISPLAY_PREFIX = "wayland-wcu"
RUNTIME_PREFIX = "wcu-headless"

# The spike's shell reached "extension answering" well inside 15 s on this
# machine; the margin covers a loaded box without making a real failure slow
# to report.
START_TIMEOUT_S = 45.0
STOP_TIMEOUT_S = 10.0

# Each session is a real compositor: ~293 MB RSS measured, plus whatever it
# is asked to run. The cap is a backstop against a loop starting sessions in
# a name space it invents, not a considered maximum -- raise it with
# WCU_HEADLESS_MAX when the RAM is there.
MAX_SESSIONS = 4
# Refuse a start that would leave the machine with less than this. A swapping
# desktop is invisible to the agent that caused it.
MIN_AVAILABLE_MB = 900

# Filesystem-safe, D-Bus-safe, display-name-safe, and short enough to read in
# a status table.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def session_name(raw: str | None = None) -> str:
    """Validate a session name, defaulting when unset.

    Names end up in a filename, a Wayland display name and a directory
    under $XDG_RUNTIME_DIR, so a bad one is refused here rather than
    producing three different confusing failures downstream.
    """
    name = (raw or DEFAULT_NAME).strip().lower()
    if not _NAME_RE.match(name):
        raise ToolError(
            f"invalid headless session name {raw!r}: use 1-32 characters of "
            "a-z, 0-9, '-' or '_', starting with a letter or digit "
            f"(default is {DEFAULT_NAME!r})", code="bad_args")
    return name


def _suffix(name: str) -> str:
    """'' for the default session, '-<name>' otherwise.

    The default keeps the paths it had before names existed, so a session
    started by the old code is still addressable by this one.
    """
    return "" if name == DEFAULT_NAME else f"-{name}"


def default_display(name: str) -> str:
    return DISPLAY_PREFIX + _suffix(name)


def _state_dir() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(state, "wayland-computer-use")
    os.makedirs(d, exist_ok=True)
    return d


def _state_file(name: str) -> str:
    return os.path.join(_state_dir(), f"headless-{name}.json")


def _legacy_state_file() -> str:
    """Where the single unnamed session recorded itself before 2026-08-27."""
    return os.path.join(_state_dir(), "headless.json")


def _read_state(name: str) -> dict[str, Any] | None:
    paths = [_state_file(name)]
    if name == DEFAULT_NAME:
        paths.append(_legacy_state_file())
    for path in paths:
        try:
            with open(path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        state.setdefault("name", name)
        return state
    return None


def known_names() -> list[str]:
    """Every session with a state file, running or stale."""
    names = set()
    for path in glob.glob(os.path.join(_state_dir(), "headless-*.json")):
        base = os.path.basename(path)
        names.add(base[len("headless-"):-len(".json")])
    if os.path.exists(_legacy_state_file()):
        names.add(DEFAULT_NAME)
    return sorted(names)


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


def _available_mb() -> int | None:
    """MemAvailable, which is the number that predicts swapping -- MemFree
    does not, because reclaimable page cache reads as used."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def status(name: str | None = None) -> dict[str, Any]:
    """What is true right now, never what the state file wishes were true."""
    name = session_name(name)
    state = _read_state(name)
    if not state:
        return {"running": False, "name": name,
                "detail": f"no headless session recorded for {name!r}"}
    shell_ok = _pid_is(int(state.get("shell_pid", -1)), "gnome-shell")
    dbus_ok = _pid_is(int(state.get("dbus_pid", -1)), "dbus-daemon")
    bus_ok = shell_ok and _bus_alive(state["bus_address"])
    ext_ok = bus_ok and _extension_answering(state["bus_address"])
    running = shell_ok and dbus_ok and bus_ok
    report: dict[str, Any] = {
        "running": running, "name": name,
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
        report["detail"] = (
            f"stale state file -- session {name!r} is gone; "
            f"`wcu-headless stop --name {name}` will clean it up, "
            f"`wcu-headless start --name {name}` will replace it")
    return report


def list_sessions() -> dict[str, Any]:
    """Every recorded session, with the totals that decide whether another
    one fits."""
    sessions = [status(n) for n in known_names()]
    live = [s for s in sessions if s["running"]]
    report: dict[str, Any] = {
        "sessions": sessions,
        "running": len(live),
        "max_sessions": _max_sessions(),
        "total_rss_mb": sum(s.get("shell_rss_mb", 0) for s in live),
    }
    available = _available_mb()
    if available is not None:
        report["available_mb"] = available
    return report


def _max_sessions() -> int:
    raw = os.environ.get("WCU_HEADLESS_MAX", "")
    try:
        return max(1, int(raw))
    except ValueError:
        return MAX_SESSIONS


class _StartLock:
    """One starter per name at a time.

    Two agent sessions calling `ensure()` for the same name within the same
    ~20 s window would otherwise both see "not running" and both spawn a
    compositor: 300 MB and a display-name collision. The loser of the race
    waits for the winner and takes its session.
    """

    def __init__(self, name: str):
        self.path = os.path.join(_state_dir(), f"headless-{name}.lock")
        self.fd: int | None = None

    def acquire(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except FileExistsError:
            if self._holder_is_dead():
                self._steal()
                return self.acquire()
            return False

    def _holder_is_dead(self) -> bool:
        try:
            with open(self.path) as f:
                pid = int(f.read().strip() or -1)
        except (OSError, ValueError):
            return True             # unreadable lock is a dead lock
        return not os.path.exists(f"/proc/{pid}")

    def _steal(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self._steal()

    def __enter__(self) -> _StartLock:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _check_capacity(name: str) -> None:
    """Refuse a start the machine cannot afford, before spending 20 s on it."""
    live = [s for s in (status(n) for n in known_names())
            if s["running"] and s["name"] != name]
    cap = _max_sessions()
    if len(live) >= cap:
        names = ", ".join(sorted(s["name"] for s in live))
        raise ToolError(
            f"{len(live)} headless sessions already running ({names}) and the "
            f"cap is {cap}. Stop one (`wcu-headless stop --name <n>`) or raise "
            "the cap with WCU_HEADLESS_MAX -- each session is a real "
            "compositor at ~300 MB.", code="bad_args")
    available = _available_mb()
    if available is not None and available < MIN_AVAILABLE_MB:
        raise ToolError(
            f"only {available} MB available and a headless session needs "
            f"~300 MB with headroom (floor is {MIN_AVAILABLE_MB} MB). Starting "
            "one here would push the machine into swap, which the agent that "
            "caused it cannot see. Close something, or stop another session.",
            code="bad_args")


def start(size: str = DEFAULT_SIZE, display: str | None = None,
          name: str | None = None) -> dict[str, Any]:
    """Bring up the private bus + headless shell, wait until the extension
    answers on it, record the result. Idempotent by refusal: a live session
    of this NAME is returned as-is, never doubled (two shells on one name
    would fight over the display name and the runtime dir)."""
    name = session_name(name)
    display = display or default_display(name)
    current = status(name)
    if current["running"]:
        current["detail"] = "already running -- reusing"
        return current

    with _StartLock(name) as lock:
        if not lock.acquire():
            # Someone else is mid-start for this name. Wait for their shell
            # rather than racing it; their session is as good as ours.
            deadline = time.monotonic() + START_TIMEOUT_S
            while time.monotonic() < deadline:
                time.sleep(0.5)
                current = status(name)
                if current["running"]:
                    current["detail"] = "started by a concurrent caller -- reusing"
                    return current
                if not os.path.exists(lock.path):
                    break           # they gave up; fall through and start it
            if not lock.acquire():
                raise ToolError(
                    f"another process is starting headless session {name!r} and "
                    f"it has not come up within {START_TIMEOUT_S:.0f}s; check "
                    f"`wcu-headless status --name {name}`", code="timeout")
        return _start_locked(name, size, display)


def _start_locked(name: str, size: str, display: str) -> dict[str, Any]:
    _check_capacity(name)
    if _read_state(name):
        _cleanup_state(name)      # stale file from a dead session

    log_path = _rotate_log(os.path.join(_state_dir(), f"headless-shell{_suffix(name)}.log"))
    # Deliberately not a context manager: this handle is the child shell's
    # stdout/stderr and has to outlive this function.
    log = open(log_path, "ab")  # noqa: SIM115

    # A PRIVATE runtime dir, because two sessions sharing one is how the
    # headless session broke the user's desktop (2026-08-24): both sessions'
    # at-spi-bus-launchers bind $XDG_RUNTIME_DIR/at-spi/bus, last bind wins
    # the path, and every primary-session app then times out registering
    # against the wrong session's a11y bus. Session-scoped sockets (a11y,
    # the wayland display itself) live here; the per-USER pipewire daemon is
    # shared back in via symlink -- mutter's ScreenCast streams (which
    # absolute pointer input rides on) need it.
    # Suffixed per name for the same reason it is private at all: two NAMED
    # headless sessions sharing one runtime dir would fight over at-spi/bus
    # exactly the way a headless session and the user's did.
    runtime_dir = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
        RUNTIME_PREFIX + _suffix(name))
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
        "name": name,
        "bus_address": address, "wayland_display": display, "size": size,
        "runtime_dir": runtime_dir,
        "shell_pid": shell.pid, "dbus_pid": dbus.pid,
        "started_at": time.time(), "log": log_path,
    }
    with open(_state_file(name), "w") as f:
        json.dump(state, f, indent=1)
    if name == DEFAULT_NAME:
        # The pre-names record would otherwise shadow this one on the next
        # read, since _read_state falls back to it.
        try:
            os.unlink(_legacy_state_file())
        except OSError:
            pass
    out = status(name)
    out["detail"] = "started"
    return out


def stop(name: str | None = None) -> dict[str, Any]:
    """End one session and remove its record. Termination order matters:
    the shell first (it is the bus client), the daemon second."""
    name = session_name(name)
    state = _read_state(name)
    if not state:
        return {"stopped": False, "name": name,
                "detail": f"no headless session recorded for {name!r}"}
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
    _cleanup_state(name)
    return {"stopped": True, "name": name, "ended": ended,
            "detail": f"headless session {name!r} ended" if ended
            else f"session {name!r} was already gone; record removed"}


def stop_all() -> dict[str, Any]:
    """End every recorded session. What to call before a reboot, or when a
    run is over and nobody knows which names it created."""
    reports = [stop(n) for n in known_names()]
    return {"stopped": any(r["stopped"] for r in reports),
            "sessions": reports,
            "detail": f"{sum(1 for r in reports if r['ended'])} session(s) ended"
            if reports else "no headless sessions recorded"}


def _cleanup_state(name: str) -> None:
    paths = [_state_file(name)]
    if name == DEFAULT_NAME:
        paths.append(_legacy_state_file())
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _rotate_log(path: str, keep_bytes: int = 2 * 1024 * 1024) -> str:
    """Keep the shell log from growing without bound.

    It was opened append-only and never truncated, and reached 854 KB on
    this machine -- 608 lines of it one repeating
    `org.gnome.SessionManager was not provided` from a headless shell that
    legitimately has no session manager. One generation is kept, because
    the only question ever asked of it is "why did the LAST start fail".
    """
    try:
        if os.path.getsize(path) > keep_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass
    return path


def ensure(size: str = DEFAULT_SIZE, display: str | None = None,
           name: str | None = None) -> dict[str, Any]:
    """Running session of this name, starting one if needed. What a pinned
    server calls at startup so `WCU_SESSION=headless[:name]` is
    self-sufficient."""
    name = session_name(name)
    current = status(name)
    if current["running"]:
        return current
    return start(size=size, display=display, name=name)


def pin_env(state: dict[str, Any], env: Any = os.environ) -> None:
    """Point THIS process at a headless session. Must run before the first
    tool call; every backend resolves the session lazily from these three
    variables, so nothing else needs to change."""
    env["DBUS_SESSION_BUS_ADDRESS"] = state["bus_address"]
    env["WAYLAND_DISPLAY"] = state["wayland_display"]
    if state.get("runtime_dir"):
        env["XDG_RUNTIME_DIR"] = state["runtime_dir"]
    # The flag input.py checks to refuse ydotool -- uinput injection lands on
    # the real seat regardless of this environment.
    env["WCU_HEADLESS"] = "1"
    # Which session, for the action journal: with several desktops live, "a
    # click happened" is not a useful record unless it says where.
    env["WCU_HEADLESS_NAME"] = state.get("name", DEFAULT_NAME)


USAGE = (
    "usage: wcu-headless [start|stop|status|list|env] [--name NAME] "
    f"[--size {DEFAULT_SIZE}] [--display NAME] [--all]\n"
    "  start   bring up a session (idempotent per name)\n"
    "  stop    end one session, or every one with --all\n"
    "  status  one session; `list` for all of them at once\n"
    "  env     shell exports that pin a SHELL to a session"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args and not args[0].startswith("-") else "status"
    size = args[args.index("--size") + 1] if "--size" in args else DEFAULT_SIZE
    display = args[args.index("--display") + 1] if "--display" in args else None
    raw_name = args[args.index("--name") + 1] if "--name" in args else None
    every = "--all" in args
    try:
        name = session_name(raw_name)
        if cmd == "start":
            report = start(size=size, display=display, name=name)
        elif cmd == "stop":
            report = stop_all() if every else stop(name)
        elif cmd == "status":
            report = status(name)
        elif cmd == "list":
            report = list_sessions()
        elif cmd == "env":
            state = _read_state(name)
            if not state or not status(name)["running"]:
                print(f"# no running headless session {name!r} -- "
                      f"`wcu-headless start --name {name}` first", file=sys.stderr)
                return 1
            print(f"export DBUS_SESSION_BUS_ADDRESS='{state['bus_address']}'")
            print(f"export WAYLAND_DISPLAY='{state['wayland_display']}'")
            if state.get("runtime_dir"):
                print(f"export XDG_RUNTIME_DIR='{state['runtime_dir']}'")
            print("export WCU_HEADLESS=1")
            print(f"export WCU_HEADLESS_NAME='{name}'")
            return 0
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except ToolError as e:
        print(e.wire_text(), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1))
    if cmd == "start" and report.get("running"):
        pin = "headless" if name == DEFAULT_NAME else f"headless:{name}"
        print(f"\n# pin a server to it:  WCU_SESSION={pin} python3 mcp_server.py",
              file=sys.stderr)
    if cmd in ("status", "list"):
        return 0
    return 0 if report.get("running") or report.get("stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
