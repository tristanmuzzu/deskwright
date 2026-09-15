#!/usr/bin/env python3
"""`deskwright-doctor` -- is the installed server actually able to work?

The difference between this and `deskwright-setup --check`: setup checks the
MACHINE before anything exists; the doctor checks the RUNNING STACK after
install and after registration, and every finding names its fix. It came out
of a bug (#3) where every static check was green, the self-test even said
16/18, and the missing thing was a D-Bus name nobody was asking about: the
a11y registry on the session the server would drive. A doctor has to ask the
live system, not the package manager.

Checks, cheap first:

  1. this is a GNOME Wayland session (else exit 2, same refusal as setup)
  2. hard dependencies -- reuses setup's probe, so install lines match
  3. the toolkit-accessibility flag
  4. the AT-SPI registry ON THIS SESSION's a11y bus (the #3 class: the socket
     answering is not the registry existing)
  5. the gnome-shell extension: files present, files current, enabled, live
  6. the server command a client would launch: version, whether it is a
     PyPI release or a local-path install, and whether it answers MCP on
     stdio (a real initialize + tools/list handshake, not an import)
  7. any running headless session: is ITS registry up (where #3 lived)
  8. `--self-test` additionally runs the headless self-test (safe: private
     virtual monitor) and reports the tally

The doctor never changes anything and never sudos. Fixes belong to
`deskwright-setup` (install/enable), the package manager (deps), or you
(the one logout gnome-shell insists on). Each finding prints the command.

Exit codes: 0 healthy, 1 something needs action, 2 not a diagnosable
target. Testability: every external read sits behind a seam
(`_a11y_registry_state`, `_mcp_probe`, `_headless_sessions`, and setup's
own seams), so the tests shell out to nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import setup_cli

REGISTRY_NAME = "org.a11y.atspi.Registry"

# Statuses, in escalating order. The label width keeps the report scannable.
OK = "ok"
WARN = "warn"          # true but worth knowing; no action to take
FIX = "fix"            # needs an action, printed with it


class Finding:
    def __init__(self, label: str, status: str, detail: str,
                 action: str | None = None):
        self.label = label
        self.status = status
        self.detail = detail
        self.action = action

    @property
    def wants_action(self) -> bool:
        return self.status == FIX


def _say(line: str = "") -> None:
    print(line)


# ------------------------------------------------------------- seams

def _a11y_registry_state() -> tuple[bool | None, str]:
    """Is the AT-SPI registry up on THIS session's a11y bus?

    Three answers: True (registry owns its name), False (socket answers, no
    registry -- the #3 state), None (could not even resolve the a11y bus).
    The distinction matters because "no socket" and "no registry" have
    different fixes, and collapsing them is how #3 hid behind the usual
    toolkit-accessibility explanation.
    """
    try:
        bus = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.a11y.Bus",
             "--object-path", "/org/a11y/bus", "--method",
             "org.a11y.Bus.GetAddress"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, "gdbus could not ask the session bus for the a11y bus"
    if bus.returncode != 0:
        return None, "org.a11y.Bus did not answer on the session bus"
    first = bus.stdout.strip().strip("()").split(",")[0].strip().strip("'\"")
    if not first.startswith("unix:path="):
        return None, f"unusable a11y bus address: {first!r}"
    socket_path = first[len("unix:path="):]
    try:
        owner = subprocess.run(
            ["gdbus", "call", "--address", first,
             "--dest", "org.freedesktop.DBus", "--object-path",
             "/org/freedesktop/DBus", "--method",
             "org.freedesktop.DBus.GetNameOwner", REGISTRY_NAME],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, f"could not ask the a11y bus at {socket_path}"
    if owner.returncode == 0:
        return True, f"registry owns {REGISTRY_NAME}"
    return False, (f"the a11y bus answers at {socket_path} but nothing owns "
                   f"{REGISTRY_NAME}: every ui_* tool will fail while apps "
                   f"look fine to every static check")


def _mcp_probe(cmd: str, timeout: float = 20.0) -> tuple[bool | None, str]:
    """Speak MCP to the server command a client is registered against.

    True/False from a real initialize + tools/list over stdio; None when the
    command could not be launched at all. This is the one check that proves
    the INSTALLED server (the client-held one may be an older process; see
    CONTRIBUTING on the mcpdrv trick).
    """
    try:
        proc = subprocess.Popen(
            [cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return None, f"could not launch {cmd!r}: {e}"
    assert proc.stdin is not None and proc.stdout is not None

    def rpc(msg: dict) -> str:
        proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        return proc.stdout.readline()  # type: ignore[union-attr]

    try:
        # A server that never answers is the common failure (import error in
        # a plugin, wrong interpreter); readline would sit forever, so the
        # whole exchange runs under a watchdog that kills the child.
        import threading
        result: dict[str, Any] = {}

        def talk() -> None:
            init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18",
                                   "capabilities": {},
                                   "clientInfo": {"name": "deskwright-doctor"}}})
            result["init"] = init
            proc.stdin.write(json.dumps(  # type: ignore[union-attr]
                {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()  # type: ignore[union-attr]
            result["tools"] = rpc({"jsonrpc": "2.0", "id": 2,
                                   "method": "tools/list", "params": {}})

        worker = threading.Thread(target=talk, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            proc.kill()
            return False, f"no MCP answer within {timeout:.0f}s (the command" \
                          f" starts but never speaks MCP)"
        init, tools = result.get("init", ""), result.get("tools", "")
        if '"serverInfo"' not in init:
            return False, f"initialize got no serverInfo: {init.strip()[:120]!r}"
        try:
            count = len(json.loads(tools)["result"]["tools"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return False, f"tools/list was not JSON with tools: {tools[:120]!r}"
        return True, f"initialize + tools/list answered, {count} tools"
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _headless_sessions() -> list[dict[str, Any]]:
    """Status of every recorded headless session (running or not)."""
    from . import headless
    return [headless.status(n) for n in headless.known_names()]


def _install_provenance(cmd: str) -> tuple[str, str]:
    """(version, provenance) of the INSTALL the command runs from.

    Asked about the command, not about this module: the doctor often runs
    from a checkout while the registered server is the pipx install, and
    those are two different codebases on a machine mid-upgrade.
    """
    ver = "unknown"
    provenance = "unknown origin"
    resolved = Path(cmd).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    # A console script lives in <prefix>/bin; its package sits in the
    # sibling site-packages. mcp_server.py in a checkout resolves to the
    # repo itself.
    for site in (resolved.parent.parent / "lib").glob("python*/site-packages"):
        dists = sorted(site.glob("deskwright*.dist-info"))
        if dists:
            # deskwright-0.1.1.dist-info -> 0.1.1 (name has no dashes, so
            # split on the FIRST dash-to-digit boundary, not rsplit)
            stem = dists[0].name[: -len(".dist-info")]
            ver = stem.split("-", 1)[1] if "-" in stem else stem
            direct = dists[0] / "direct_url.json"
            if direct.is_file():
                try:
                    url = json.loads(direct.read_text()).get("url", "")
                except (OSError, json.JSONDecodeError):
                    url = ""
                if url.startswith("file://"):
                    provenance = f"local path install ({url[len('file://'):]})"
                elif url:
                    provenance = f"direct install from {url[:60]}"
                else:
                    provenance = "installed from a direct URL, not a PyPI release"
            else:
                provenance = "PyPI release"
            return ver, provenance
    if resolved.name == "mcp_server.py":
        return ver, f"checkout ({resolved.parent})"
    return ver, f"unrecognized layout at {resolved}"


# ------------------------------------------------------------- checks

def check_session() -> list[Finding]:
    why = setup_cli.check_desktop()
    if why:
        return [Finding("session", FIX, why,
                        "deskwright targets GNOME Wayland; nothing else to"
                        " check here.")]
    return [Finding("session", OK, "GNOME Wayland")]


def check_deps() -> list[Finding]:
    lines, missing = setup_cli.probe_deps(setup_cli.detect_host_family())
    for line in lines:
        _say("  " + line)
    if not missing:
        return [Finding("dependencies", OK, "all hard requirements present")]
    return [Finding(
        "dependencies", FIX,
        f"{len(missing)} hard requirement(s) missing",
        "install: " + " && ".join(
            setup_cli.install_line(setup_cli.detect_host_family(), d.pkgs)
            for d in missing)
        + ", then re-run deskwright-doctor")]


def check_a11y_flag() -> Finding:
    value = setup_cli._gsettings_get(setup_cli.A11Y_SCHEMA, setup_cli.A11Y_KEY)
    if value is None:
        return Finding(
            "toolkit-accessibility", FIX,
            setup_cli._gsettings_why(setup_cli.A11Y_SCHEMA, setup_cli.A11Y_KEY),
            f"gsettings set {setup_cli.A11Y_SCHEMA} {setup_cli.A11Y_KEY} true"
            "  (or just run deskwright-setup)")
    if value == "true":
        return Finding("toolkit-accessibility", OK, "true")
    return Finding(
        "toolkit-accessibility", FIX, f"currently {value}",
        f"gsettings set {setup_cli.A11Y_SCHEMA} {setup_cli.A11Y_KEY} true,"
        " then restart running apps (they read it at startup)")


def check_registry() -> Finding:
    state, detail = _a11y_registry_state()
    if state is True:
        return Finding("AT-SPI registry (this session)", OK, detail)
    if state is False:
        # The #3 signature. On the primary session systemd usually has this
        # covered; if it is down HERE, at-spi-dbus-bus.service is the place
        # to look, and the headless variant of this bug is issue #3.
        return Finding(
            "AT-SPI registry (this session)", FIX, detail,
            "systemctl --user restart at-spi-dbus-bus.service, then log out"
            " and back in; if this is a headless session, that is issue #3")
    return Finding("AT-SPI registry (this session)", FIX, detail,
                   "check that at-spi2-core is installed and the session"
                   " bus is reachable (gdbus call --session --dest"
                   " org.freedesktop.DBus --object-path /org/freedesktop/DBus"
                   " --method org.freedesktop.DBus.GetId)")


def check_extension() -> list[Finding]:
    src = setup_cli.find_extension_source(None)
    dest = setup_cli.EXTENSIONS_DIR / setup_cli.EXTENSION_UUID
    out: list[Finding] = []
    if src is None:
        out.append(Finding(
            "extension files", FIX,
            "the bundled extension source is missing from this install",
            "reinstall the package: the wheel carries the extension"))
    else:
        current = dest.is_dir() and setup_cli._dirs_identical(src, dest)
        out.append(Finding(
            "extension files", OK if current else FIX,
            "installed and current" if current else
            (f"stale copy at {dest}" if dest.is_dir()
             else f"not installed at {dest}"),
            None if current else "deskwright-setup   (copies + enables it)"))
    enabled = setup_cli._gsettings_get(setup_cli.SHELL_SCHEMA,
                                       setup_cli.ENABLED_KEY)
    in_list = (enabled is not None
               and setup_cli.EXTENSION_UUID in setup_cli.parse_string_list(enabled))
    out.append(Finding(
        "extension enabled", OK if in_list else FIX,
        "in enabled-extensions" if in_list else "not in enabled-extensions",
        None if in_list else "deskwright-setup"))
    live = setup_cli._bus_has_owner("com.zeticle.deskwright")
    if live:
        out.append(Finding("extension live", OK,
                           "answering on D-Bus (com.zeticle.deskwright)"))
    elif live is False:
        out.append(Finding(
            "extension live", FIX,
            "files may be in place but the compositor is not running them",
            "log out and log back in -- on Wayland an extension cannot be"
            " loaded into a running shell. Until then the server works via"
            " fallbacks (no window verbs, no halt key)."))
    else:
        out.append(Finding("extension live", WARN,
                           "could not ask the session bus"))
    return out


def check_server() -> list[Finding]:
    cmd, caveat = setup_cli.server_command()
    ver, provenance = _install_provenance(cmd)
    out = [Finding("server command", OK, f"{cmd} ({ver}, {provenance})")]
    if "not found" in (caveat or ""):
        out.append(Finding("server command", FIX, caveat or "",
                           "pipx install --system-site-packages deskwright"))
    elif caveat:
        out.append(Finding("server command", WARN, caveat))
    works, detail = _mcp_probe(cmd)
    if works:
        out.append(Finding("MCP over stdio", OK, detail))
    elif works is False:
        out.append(Finding(
            "MCP over stdio", FIX, detail,
            "the command runs but is not a working MCP server -- reinstall"
            " it (pipx install --force --system-site-packages deskwright)"
            " and re-register the client"))
    else:
        out.append(Finding(
            "MCP over stdio", FIX, detail,
            "the registered command does not launch; fix the registration"
            " to point at a real path"))
    return out


def check_headless() -> list[Finding]:
    out: list[Finding] = []
    try:
        sessions = _headless_sessions()
    except Exception as e:
        return [Finding("headless sessions", WARN,
                        f"could not enumerate: {type(e).__name__}: {e}")]
    if not sessions:
        return [Finding("headless sessions", OK,
                        "none recorded (start one with"
                        " DESKWRIGHT_SESSION=headless, or"
                        " deskwright-headless start)")]
    for st in sessions:
        name = st.get("name", "?")
        if not st.get("running"):
            out.append(Finding(f"headless:{name}", WARN,
                               "recorded but not running (stale state file)"
                               if "stale" in st.get("detail", "")
                               else st.get("detail", "not running")))
            continue
        from . import headless
        state, detail = (True, "registry up") if headless._registry_alive(
            st["bus_address"]) else (False, "no registry on its a11y bus")
        if state:
            out.append(Finding(f"headless:{name}", OK,
                               f"running, {detail}"))
        else:
            out.append(Finding(
                f"headless:{name}", FIX,
                f"running but {detail} -- ui_* tools will fail on it",
                f"deskwright-headless stop --name {name} &&"
                f" deskwright-headless start --name {name}"
                f"   (a pre-#3-fix server started it; restart brings the"
                f" registry)"))
    return out


def run_self_test(cmd: str) -> Finding:
    """The headless self-test: safe to run unattended (private virtual
    monitor), ~20 s and ~300 MB the first time."""
    _say("  running the headless self-test (a second gnome-shell on a"
         " virtual monitor; your screen is not touched)...")
    try:
        proc = subprocess.run(
            [cmd, "--self-test"], env=dict(os.environ,
                                           DESKWRIGHT_SESSION="headless"),
            capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Finding("headless self-test", FIX,
                       f"could not run: {type(e).__name__}: {e}")
    tail = [ln for ln in proc.stdout.splitlines()
            if ln.endswith("passed")][-1:]
    tally = tail[0].strip() if tail else f"rc={proc.returncode}"
    fails = [ln for ln in proc.stdout.splitlines() if ln.startswith("FAIL")]
    ok = proc.returncode == 0
    return Finding(
        "headless self-test", OK if ok else FIX,
        tally + ("" if ok else "; " + "; ".join(
            f.split(None, 1)[-1][:80] for f in fails[:3])),
        None if ok else
        "the failing lines above name the capability;"
        " deskwright-setup --check re-checks the machine")


# ------------------------------------------------------------- report

def _emit(f: Finding) -> None:
    """One finding, printed where it belongs: under its section header."""
    mark = {"ok": "ok", "warn": "note", "fix": "FIX"}[f.status]
    _say(f"[{mark}] {f.label}: {f.detail}")
    if f.action:
        _say(f"       action: {f.action}")


def _emit_all(findings: list[Finding]) -> list[Finding]:
    """Print a section's findings and hand them back for the summary."""
    for f in findings:
        _emit(f)
    return findings


def _render(findings: list[Finding]) -> int:
    """The summary block. Sections above already printed their details."""
    _say()
    actions = sum(1 for f in findings if f.wants_action)
    if actions:
        _say(f"RESULT: {actions} thing(s) need action (commands above)."
             " Re-run deskwright-doctor afterwards.")
        return 1
    warns = sum(1 for f in findings if f.status == WARN)
    _say("RESULT: healthy." + (f" {warns} note(s), no action needed."
                               if warns else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    _say("deskwright-doctor -- diagnosing the installed stack, changing"
         " nothing")

    session = check_session()
    if session[0].wants_action:
        # Not a diagnosable target: say exactly this and nothing else, the
        # same refusal shape as setup's --check.
        for f in session:
            _emit(f)
        _say("exit 2: this machine is not a deskwright target")
        return 2

    findings: list[Finding] = list(session)
    _say()
    _say("== session ==")
    _emit_all(session)
    _say()
    _say("== dependencies ==")
    findings += _emit_all(check_deps())
    _say()
    _say("== accessibility ==")
    findings += _emit_all([check_a11y_flag(), check_registry()])
    _say()
    _say("== gnome-shell extension ==")
    findings += _emit_all(check_extension())
    _say()
    _say("== the server a client launches ==")
    findings += _emit_all(check_server())
    _say()
    _say("== headless sessions ==")
    findings += _emit_all(check_headless())

    if "--self-test" in argv:
        _say()
        _say("== self-test ==")
        cmd, _ = setup_cli.server_command()
        findings += _emit_all([run_self_test(cmd)])

    return _render(findings)


if __name__ == "__main__":
    sys.exit(main())
