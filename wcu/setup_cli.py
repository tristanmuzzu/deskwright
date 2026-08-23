#!/usr/bin/env python3
"""One-command setup for a fresh machine (`wcu-setup`).

What a new user needs, in the order they need it:

  1. system/python dependencies -- detected, with the exact install line per
     missing item for their distro family (never run; this script never sudos)
  2. `toolkit-accessibility` gsettings key -- set idempotently, before/after
     printed, with the warning that already-running apps stay stunted
  3. the bundled gnome-shell extension -- copied into place and enabled via
     the org.gnome.shell enabled-extensions gsettings list (the
     `gnome-extensions enable` CLI answers "does not exist" for an extension
     the shell has not scanned yet, i.e. any freshly copied one -- verified
     2026-08-23; the gsettings append is the route that works), then a loud
     LOG OUT / LOG IN notice
  4. the optional ydotoold system service -- unit content and commands are
     PRINTED, never run (they need root)
  5. the `claude mcp add` registration line (the MCP server itself stays
     path-invoked; see docs/claude-code-setup.md)

`--check` runs every detection read-only and exits nonzero if a hard
requirement is missing. The apply mode is idempotent: run it twice and the
second run changes nothing and says so. Every state change prints the value
before and after.

Testability: every external read goes through a module-level seam
(`_which`, `_py_import_ok`, `_gsettings_get`, `_bus_has_owner`) so the tests
monkeypatch those and never touch the real machine.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXTENSION_UUID = "wcu@wayland-computer-use"
EXTENSIONS_DIR = Path.home() / ".local/share/gnome-shell/extensions"
A11Y_SCHEMA = "org.gnome.desktop.interface"
A11Y_KEY = "toolkit-accessibility"
SHELL_SCHEMA = "org.gnome.shell"
ENABLED_KEY = "enabled-extensions"
DISABLED_KEY = "disabled-extensions"
YDOTOOLD_SOCKET = "/run/ydotoold.socket"

# ---------------------------------------------------------------- distro

_FAMILY_OF_ID = {
    "debian": "debian", "ubuntu": "debian", "linuxmint": "debian",
    "pop": "debian", "raspbian": "debian", "elementary": "debian",
    "fedora": "fedora", "rhel": "fedora", "centos": "fedora",
    "rocky": "fedora", "almalinux": "fedora", "nobara": "fedora",
    "arch": "arch", "manjaro": "arch", "endeavouros": "arch",
    "cachyos": "arch",
}

_INSTALL_FMT = {
    "debian": "sudo apt install -y {}",
    "fedora": "sudo dnf install -y {}",
    "arch": "sudo pacman -S --needed {}",
}


def detect_family(os_release_text: str) -> str:
    """Distro family ("debian"/"fedora"/"arch") from os-release content.

    Falls back to ID_LIKE when ID itself is unknown, and to "unknown" when
    neither matches -- callers phrase install lines as apt in that case.
    """
    fields: dict[str, str] = {}
    for line in os_release_text.splitlines():
        m = re.match(r'^\s*([A-Z_]+)=("?)(.*)\2\s*$', line)
        if m:
            fields[m.group(1)] = m.group(3)
    if fields.get("ID", "").lower() in _FAMILY_OF_ID:
        return _FAMILY_OF_ID[fields["ID"].lower()]
    for token in fields.get("ID_LIKE", "").lower().split():
        if token in _FAMILY_OF_ID:
            return _FAMILY_OF_ID[token]
    return "unknown"


def detect_host_family() -> str:
    try:
        return detect_family(Path("/etc/os-release").read_text())
    except OSError:
        return "unknown"


def install_line(family: str, pkgs: dict[str, str]) -> str:
    """The exact command to install one dependency on this distro family."""
    if family in _INSTALL_FMT:
        return _INSTALL_FMT[family].format(pkgs[family])
    return (_INSTALL_FMT["debian"].format(pkgs["debian"])
            + "   (distro not recognized; apt phrasing shown)")


# ---------------------------------------------------------------- deps

@dataclass(frozen=True)
class Dep:
    label: str          # human name shown in the report
    kind: str           # "py" (import into system python3) or "bin" (on PATH)
    target: str         # module name or binary name
    hard: bool          # missing => --check exits nonzero
    pkgs: dict[str, str]  # family -> package name
    why: str            # one line of purpose


DEPS: tuple[Dep, ...] = (
    Dep("PyGObject (python3 module 'gi')", "py", "gi", True,
        {"debian": "python3-gi", "fedora": "python3-gobject",
         "arch": "python-gobject"},
        "talks to org.gnome.Mutter.RemoteDesktop (pointer, keys, screencast);"
        " a system package, not sanely pip-installable"),
    Dep("Pillow (python3 module 'PIL')", "py", "PIL", True,
        {"debian": "python3-pil", "fedora": "python3-pillow",
         "arch": "python-pillow"},
        "crops, scales and annotates screenshots"),
    Dep("gdbus (GLib CLI)", "bin", "gdbus", True,
        {"debian": "libglib2.0-bin", "fedora": "glib2", "arch": "glib2"},
        "every call to the gnome-shell extension goes through it"),
    Dep("wl-clipboard (wl-copy / wl-paste)", "bin", "wl-paste", True,
        {"debian": "wl-clipboard", "fedora": "wl-clipboard",
         "arch": "wl-clipboard"},
        "clipboard read, and the clipboard-write fallback"),
    Dep("tesseract OCR", "bin", "tesseract", True,
        {"debian": "tesseract-ocr", "fedora": "tesseract",
         "arch": "tesseract"},
        "find_text and screenshot OCR (no pytesseract needed -- the binary"
        " is called directly)"),
    Dep("ydotool (OPTIONAL fallback)", "bin", "ydotool", False,
        {"debian": "ydotool", "fedora": "ydotool", "arch": "ydotool"},
        "input fallback only; the RemoteDesktop path does not need it"),
)


# Seams the tests monkeypatch -- keep all external reads behind these. -------

def _which(name: str) -> str | None:
    return shutil.which(name)


def _py_import_ok(module: str) -> bool:
    """Import-probe the SYSTEM python3 (the one mcp_server.py's shebang runs),
    not necessarily the interpreter running this script (uvx/pipx venvs do not
    see distro packages like python3-gi)."""
    py = shutil.which("python3")
    if not py:
        return False
    try:
        return subprocess.run(
            [py, "-c", f"import {module}"],
            capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gsettings_get(schema: str, key: str) -> str | None:
    if not shutil.which("gsettings"):
        return None
    try:
        proc = subprocess.run(["gsettings", "get", schema, key],
                              capture_output=True, text=True, timeout=15,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _bus_has_owner(name: str) -> bool | None:
    """True/False from the session bus; None when it cannot be asked."""
    if not shutil.which("gdbus"):
        return None
    try:
        proc = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.NameHasOwner", name],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return "true" in proc.stdout.lower()


# ---------------------------------------------------------------- report

def _say(line: str = "") -> None:
    print(line)


def _header(title: str) -> None:
    _say()
    _say(f"== {title} ==")


def probe_deps(family: str) -> tuple[list[str], list[Dep]]:
    """Print one line per dependency; return (report_lines, missing_hard)."""
    lines: list[str] = []
    missing_hard: list[Dep] = []
    for dep in DEPS:
        found = (_py_import_ok(dep.target) if dep.kind == "py"
                 else _which(dep.target) is not None)
        if found:
            lines.append(f"  [ok]       {dep.label}")
        elif dep.hard:
            missing_hard.append(dep)
            lines.append(f"  [MISSING]  {dep.label} -- {dep.why}")
            lines.append(f"             install: {install_line(family, dep.pkgs)}")
        else:
            lines.append(f"  [absent, optional]  {dep.label} -- {dep.why}")
            lines.append(f"             install: {install_line(family, dep.pkgs)}")
    return lines, missing_hard


# ------------------------------------------------------- gvariant lists

def parse_string_list(gvariant: str) -> list[str]:
    """GVariant `as` text (`['a', 'b']`, `@as []`) -> python list."""
    s = gvariant.strip()
    if s.startswith("@as"):
        s = s[3:].strip()
    return re.findall(r"'([^']*)'", s)


def format_string_list(items: list[str]) -> str:
    return "[" + ", ".join(f"'{i}'" for i in items) + "]"


def list_with_uuid(gvariant: str, uuid: str) -> str | None:
    """New GVariant text with uuid appended, or None when already present."""
    items = parse_string_list(gvariant)
    if uuid in items:
        return None
    return format_string_list(items + [uuid])


def list_without_uuid(gvariant: str, uuid: str) -> str | None:
    """New GVariant text with uuid removed, or None when absent."""
    items = parse_string_list(gvariant)
    if uuid not in items:
        return None
    return format_string_list([i for i in items if i != uuid])


# ---------------------------------------------------------------- repo

def find_repo_root(explicit: str | None) -> Path | None:
    """The checkout that holds mcp_server.py and the bundled extension."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parent.parent)
    for c in candidates:
        if (c / "mcp_server.py").is_file() and \
                (c / "extension" / EXTENSION_UUID / "metadata.json").is_file():
            return c.resolve()
    return None


def _dirs_identical(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(_dirs_identical(a / d, b / d) for d in cmp.common_dirs)


# ---------------------------------------------------------------- steps

def _gsettings_set(schema: str, key: str, value: str) -> bool:
    proc = subprocess.run(["gsettings", "set", schema, key, value],
                          capture_output=True, text=True, timeout=15,
                          check=False)
    if proc.returncode != 0:
        _say(f"  gsettings set FAILED: {proc.stderr.strip()}")
    return proc.returncode == 0


def step_accessibility(check_only: bool) -> None:
    _header("toolkit-accessibility (AT-SPI trees)")
    before = _gsettings_get(A11Y_SCHEMA, A11Y_KEY)
    if before is None:
        _say("  cannot read it -- gsettings unavailable (not a GNOME session?)")
        return
    _say(f"  before: {A11Y_SCHEMA} {A11Y_KEY} = {before}")
    if before == "true":
        _say("  already true -- nothing to change.")
    elif check_only:
        _say(f"  would set it to true (run without --check, or:"
             f" gsettings set {A11Y_SCHEMA} {A11Y_KEY} true)")
    else:
        if _gsettings_set(A11Y_SCHEMA, A11Y_KEY, "true"):
            after = _gsettings_get(A11Y_SCHEMA, A11Y_KEY)
            _say(f"  after:  {A11Y_SCHEMA} {A11Y_KEY} = {after}")
    _say("  NOTE: applications read this at startup. Anything ALREADY RUNNING"
         " keeps a stunted")
    _say("  accessibility tree (reads as 'this app has no widgets') until"
         " that app restarts.")


def step_extension(repo: Path | None, check_only: bool) -> None:
    _header("gnome-shell extension (compositor-side helpers)")
    if repo is None:
        _say("  repo checkout not found -- run from the clone or pass"
             " --repo /path/to/wayland-computer-use.")
        _say("  (The extension and the MCP server are used from the checkout;"
             " there is nothing to install without it.)")
        return

    src = repo / "extension" / EXTENSION_UUID
    dest = EXTENSIONS_DIR / EXTENSION_UUID
    installed = dest.is_dir()
    up_to_date = installed and _dirs_identical(src, dest)

    enabled_now = _gsettings_get(SHELL_SCHEMA, ENABLED_KEY)
    in_enabled = (enabled_now is not None
                  and EXTENSION_UUID in parse_string_list(enabled_now))
    disabled_now = _gsettings_get(SHELL_SCHEMA, DISABLED_KEY)
    in_disabled = (disabled_now is not None
                   and EXTENSION_UUID in parse_string_list(disabled_now))
    live = _bus_has_owner("org.wcu.Helpers")

    if check_only:
        _say(f"  files:   {'up to date' if up_to_date else 'stale copy' if installed else 'not installed'}"
             f" at {dest}")
        _say(f"  enabled: {'yes' if in_enabled else 'no'}"
             f" (in {SHELL_SCHEMA} {ENABLED_KEY})"
             + ("; ALSO in disabled-extensions -- remove it there" if in_disabled else ""))
        _say(f"  live on D-Bus (org.wcu.Helpers): "
             f"{'yes' if live else 'no -- needs a log out/in after install' if live is not None else 'could not ask the session bus'}")
        return

    # 1. copy the files (idempotent)
    if up_to_date:
        _say(f"  files already installed and up to date at {dest}")
    else:
        _say(f"  before: {dest} {'exists (stale)' if installed else 'absent'}")
        _say(f"  copying {src} -> {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        _say(f"  after:  {dest} installed"
             f" ({sum(1 for p in dest.rglob('*') if p.is_file())} files)")

    # 2. enable. `gnome-extensions enable` answers "does not exist" for an
    # extension the shell has not scanned yet (any freshly copied one), so the
    # gsettings enabled-extensions append is the route that actually works;
    # try the CLI first anyway for the already-scanned case.
    cli_ok = False
    if _which("gnome-extensions"):
        proc = subprocess.run(
            ["gnome-extensions", "enable", EXTENSION_UUID],
            capture_output=True, text=True, timeout=15, check=False)
        cli_ok = proc.returncode == 0
        if not cli_ok:
            _say(f"  `gnome-extensions enable` refused"
                 f" ({(proc.stderr or proc.stdout).strip() or 'no reason given'})"
                 f" -- normal for a fresh copy; using gsettings instead.")
    if enabled_now is None:
        _say("  cannot enable: gsettings unavailable.")
    else:
        new = list_with_uuid(_gsettings_get(SHELL_SCHEMA, ENABLED_KEY) or "[]",
                             EXTENSION_UUID)
        if new is None:
            _say(f"  already in {ENABLED_KEY} -- nothing to change.")
        else:
            _say(f"  before: {SHELL_SCHEMA} {ENABLED_KEY} = {enabled_now}")
            if _gsettings_set(SHELL_SCHEMA, ENABLED_KEY, new):
                _say(f"  after:  {SHELL_SCHEMA} {ENABLED_KEY} ="
                     f" {_gsettings_get(SHELL_SCHEMA, ENABLED_KEY)}")
        if in_disabled and disabled_now is not None:
            trimmed = list_without_uuid(disabled_now, EXTENSION_UUID)
            if trimmed is not None:
                _say(f"  before: {SHELL_SCHEMA} {DISABLED_KEY} = {disabled_now}")
                if _gsettings_set(SHELL_SCHEMA, DISABLED_KEY, trimmed):
                    _say(f"  after:  {SHELL_SCHEMA} {DISABLED_KEY} ="
                         f" {_gsettings_get(SHELL_SCHEMA, DISABLED_KEY)}")

    if live:
        _say("  extension is already live on D-Bus (org.wcu.Helpers) --"
             " no logout needed.")
    else:
        _say()
        _say("  " + "*" * 66)
        _say("  *  LOG OUT AND LOG BACK IN -- this is not optional.           *")
        _say("  *  On Wayland gnome-shell cannot reload an extension in       *")
        _say("  *  place; the copied code is not running until next login.    *")
        _say("  *                                                             *")
        _say("  *  Until then: the MCP server already works via its           *")
        _say("  *  fallbacks (AT-SPI widgets, RemoteDesktop pointer/keys,     *")
        _say("  *  wl-clipboard). After login you additionally get window     *")
        _say("  *  verbs (list/raise/move/close), extension screenshots,      *")
        _say("  *  pointer position, and the human halt key                   *")
        _say("  *  (<Super><Ctrl>Escape).                                     *")
        _say("  " + "*" * 66)


def step_ydotoold() -> None:
    _header("ydotoold system service (OPTIONAL -- fallback input only)")
    _say("  Only the `via: \"ydotool\"` fallback needs this; the default"
         " RemoteDesktop path does not.")
    _say("  It needs root, so nothing is run here -- if you want it, the unit"
         " and commands are:")
    _say()
    uid, gid = os.getuid(), os.getgid()
    _say("  # /etc/systemd/system/ydotoold.service")
    _say("  [Unit]")
    _say("  Description=ydotoold user-input daemon (uinput)")
    _say("  [Service]")
    _say(f"  ExecStart=/usr/bin/ydotoold"
         f" --socket-path={YDOTOOLD_SOCKET} --socket-own={uid}:{gid}")
    _say("  Restart=on-failure")
    _say("  [Install]")
    _say("  WantedBy=multi-user.target")
    _say()
    _say("  sudo systemctl daemon-reload")
    _say("  sudo systemctl enable --now ydotoold.service")
    _say(f"  # ({uid}:{gid} is your uid:gid -- use `id -u`/`id -g` on the"
         " target user)")
    has_socket = Path(YDOTOOLD_SOCKET).exists()
    _say(f"  current state: socket {YDOTOOLD_SOCKET}"
         f" {'present' if has_socket else 'absent'}")


def step_mcp(repo: Path | None) -> None:
    _header("Claude Code registration (printed, not run)")
    where = str(repo) if repo else "/path/to/wayland-computer-use"
    _say(f"  cd {where}")
    _say('  claude mcp add wayland-computer-use --scope user --'
         ' "$PWD/mcp_server.py"')
    _say("  The MCP server itself stays path-invoked from the checkout --"
         " it is not wrapped by this package.")
    _say("  Auto-approval allowlist and the reasoning behind it:"
         " docs/claude-code-setup.md")


def step_self_test(repo: Path | None) -> None:
    _header("proving it works")
    if repo:
        _say(f"  {repo}/mcp_server.py --self-test")
    else:
        _say("  ./mcp_server.py --self-test   (from the checkout)")
    _say("  Not run by this script: the self-test injects input (it probes the"
         " key-combo guards),")
    _say("  so run it yourself when you are looking at the screen.")


# ---------------------------------------------------------------- main

def run(check_only: bool, repo_arg: str | None) -> int:
    family = detect_host_family()
    repo = find_repo_root(repo_arg)
    _say("wcu-setup"
         + (" --check (read-only, changes nothing)" if check_only else "")
         + f" -- distro family: {family}"
         + (f" -- repo: {repo}" if repo else " -- repo: NOT FOUND"))

    _header("dependencies")
    lines, missing_hard = probe_deps(family)
    for line in lines:
        _say(line)

    step_accessibility(check_only)
    step_extension(repo, check_only)
    step_ydotoold()
    step_mcp(repo)
    step_self_test(repo)

    _say()
    if missing_hard:
        _say(f"RESULT: {len(missing_hard)} hard requirement(s) missing --"
             " install them (lines above), then re-run wcu-setup.")
        return 1
    _say("RESULT: all hard requirements present."
         + ("" if check_only else " Setup steps applied (idempotent -- safe"
            " to run again)."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wcu-setup",
        description="Set up wayland-computer-use on a fresh machine. Never"
                    " sudos; root-needing steps are printed, not run."
                    " Safe to run repeatedly.")
    parser.add_argument("--check", action="store_true",
                        help="read-only: detect everything, change nothing,"
                             " exit nonzero if a hard requirement is missing")
    parser.add_argument("--repo", metavar="DIR", default=None,
                        help="path to the wayland-computer-use checkout"
                             " (default: autodetect from cwd / this file)")
    args = parser.parse_args(argv)
    return run(check_only=args.check, repo_arg=args.repo)


if __name__ == "__main__":
    sys.exit(main())
