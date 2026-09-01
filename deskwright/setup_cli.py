#!/usr/bin/env python3
"""One-command setup for a fresh machine (`deskwright-setup`).

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
(`_which`, `_import_ok_here`, `_import_ok_system`, `_gsettings_get`,
`_bus_has_owner`) so the tests
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

from .config import EXTENSION_DIR

EXTENSION_UUID = "deskwright@zeticle.com"
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
    Dep("AT-SPI GObject typelib (gi 'Atspi')", "typelib", "Atspi", True,
        {"debian": "gir1.2-atspi-2.0", "fedora": "at-spi2-core",
         "arch": "at-spi2-core"},
        "the whole ui_* surface -- ui_find / ui_press / ui_tree / ui_set_text."
        " python3-gi alone is not enough; the typelib is a separate package"),
    Dep("gnome-shell", "bin", "gnome-shell", False,
        {"debian": "gnome-shell", "fedora": "gnome-shell",
         "arch": "gnome-shell"},
        "only the headless second session needs it as a BINARY; on a normal"
        " desktop it is already running"),
    Dep("dbus-daemon", "bin", "dbus-daemon", False,
        {"debian": "dbus-bin", "fedora": "dbus-daemon", "arch": "dbus"},
        "only the headless second session needs it -- it runs a private bus."
        " Fedora defaults to dbus-broker, which is not a substitute here"),
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


def _typelib_ok(namespace: str) -> bool:
    """Is a GObject-Introspection typelib installed?

    `import gi` succeeding says nothing about whether `Atspi` is there --
    they are separate distro packages, and reporting only the first earned a
    green check on machines where every `ui_*` tool fails with
    "Namespace Atspi not available", which names no package to install.
    """
    for py in (sys.executable, shutil.which("python3")):
        if not py:
            continue
        try:
            ok = subprocess.run(
                [py, "-c", f"import gi; gi.require_version({namespace!r}, '2.0')"],
                capture_output=True, timeout=20, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return True
    return False


def _import_ok_here(module: str) -> bool:
    """Can THIS interpreter import it? This is the one that matters for a
    pip/pipx install: the console script runs under `sys.executable`, so a
    venv built without `--system-site-packages` cannot see python3-gi no
    matter what the distro python has."""
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _import_ok_system(module: str) -> bool:
    """Can the system python3 import it? This is the interpreter the
    checkout's `./mcp_server.py` shebang and the Claude Code plugin route
    use, so it still has to be reported."""
    py = shutil.which("python3")
    if not py:
        return False
    try:
        return subprocess.run(
            [py, "-c", f"import {module}"],
            capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _py_import_ok(module: str) -> bool:
    """Present for either route the server can be started by.

    Reporting only one of them is how `pipx install` WITHOUT
    `--system-site-packages` used to earn a green check: the distro python
    has python3-gi, the venv does not, and the server that actually runs is
    the venv's. `probe_deps` says which route is missing it.
    """
    return _import_ok_here(module) or _import_ok_system(module)


def _gsettings_get(schema: str, key: str) -> str | None:
    """The key's value, or None. `_gsettings_why` says which None this is."""
    return _gsettings_read(schema, key)[0]


def _gsettings_why(schema: str, key: str) -> str:
    """Why a read returned None, in words a user can act on.

    Collapsing "gsettings is missing" and "that schema is not installed" into
    one None produced `cannot enable: gsettings unavailable` on a KDE box
    three lines after gsettings had successfully written another key.
    """
    return _gsettings_read(schema, key)[1]


def _gsettings_read(schema: str, key: str) -> tuple[str | None, str]:
    if not shutil.which("gsettings"):
        return None, "the gsettings binary is not installed"
    try:
        proc = subprocess.run(["gsettings", "get", schema, key],
                              capture_output=True, text=True, timeout=15,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, "gsettings did not answer"
    if proc.returncode == 0:
        return proc.stdout.strip(), ""
    err = (proc.stderr or "").strip()
    if "No such schema" in err:
        return None, (f"the {schema} schema is not installed -- "
                      "this machine is not running GNOME Shell")
    return None, (err.splitlines()[0] if err else "gsettings returned an error")


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
        if dep.kind == "typelib":
            here = system = found = _typelib_ok(dep.target)
        elif dep.kind == "py":
            here, system = _import_ok_here(dep.target), _import_ok_system(dep.target)
            found = here or system
        else:
            here = system = found = _which(dep.target) is not None
        if found:
            lines.append(f"  [ok]       {dep.label}")
            if dep.kind == "py" and system and not here:
                # The distro python has it and this one does not. Every tool
                # that needs it will fail at the first call with a green
                # setup report behind it, so name the fix here.
                lines.append(
                    f"             NOTE: the system python3 has {dep.target!r}"
                    f" but {sys.executable} does not.")
                lines.append(
                    "             The console script runs under the latter,"
                    " so reinstall letting it see distro packages:")
                lines.append(
                    "               pipx install --system-site-packages"
                    " deskwright")
                lines.append(
                    "             (Fine to ignore if you start the server from"
                    " a checkout or the Claude Code plugin.)")
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
    return format_string_list([*items, uuid])


def list_without_uuid(gvariant: str, uuid: str) -> str | None:
    """New GVariant text with uuid removed, or None when absent."""
    items = parse_string_list(gvariant)
    if uuid not in items:
        return None
    return format_string_list([i for i in items if i != uuid])


# ------------------------------------------------------- extension source

def find_extension_source(explicit: str | None) -> Path | None:
    """Where the gnome-shell extension is read FROM.

    It ships inside the `deskwright` package (`deskwright/extension/<uuid>/`), so a wheel
    installed from PyPI carries it and there is nothing to clone. `--repo`
    stays as an override for anyone testing an edited copy: pass either a
    checkout, or the extension directory itself.
    """
    def valid(p: Path) -> Path | None:
        return p.resolve() if (p / "metadata.json").is_file() else None

    if explicit:
        base = Path(explicit).expanduser()
        for cand in (base,
                     base / EXTENSION_UUID,
                     base / "extension" / EXTENSION_UUID,
                     base / "deskwright" / "extension" / EXTENSION_UUID):
            found = valid(cand)
            if found:
                return found
        return None
    return valid(EXTENSION_DIR / EXTENSION_UUID)


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
        _say(f"  cannot read it -- {_gsettings_why(A11Y_SCHEMA, A11Y_KEY)}.")
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


def step_extension(src: Path | None, check_only: bool) -> None:
    _header("gnome-shell extension (compositor-side helpers)")
    if src is None:
        _say("  extension source not found -- the copy bundled with this"
             " install is missing, and --repo did not point at one either.")
        _say(f"  (Expected it at {EXTENSION_DIR / EXTENSION_UUID}.)")
        return

    dest = EXTENSIONS_DIR / EXTENSION_UUID
    installed = dest.is_dir()
    up_to_date = installed and _dirs_identical(src, dest)

    enabled_now = _gsettings_get(SHELL_SCHEMA, ENABLED_KEY)
    in_enabled = (enabled_now is not None
                  and EXTENSION_UUID in parse_string_list(enabled_now))
    disabled_now = _gsettings_get(SHELL_SCHEMA, DISABLED_KEY)
    in_disabled = (disabled_now is not None
                   and EXTENSION_UUID in parse_string_list(disabled_now))
    live = _bus_has_owner("com.zeticle.deskwright")

    if check_only:
        _say(f"  files:   {'up to date' if up_to_date else 'stale copy' if installed else 'not installed'}"
             f" at {dest}")
        _say(f"  enabled: {'yes' if in_enabled else 'no'}"
             f" (in {SHELL_SCHEMA} {ENABLED_KEY})"
             + ("; ALSO in disabled-extensions -- remove it there" if in_disabled else ""))
        _say(f"  live on D-Bus (com.zeticle.deskwright): "
             f"{'yes' if live else 'no -- needs a log out/in after install' if live is not None else 'could not ask the session bus'}")
        return

    # 1. copy the files (idempotent)
    if up_to_date:
        _say(f"  files already installed and up to date at {dest}")
    else:
        _say(f"  before: {dest} {'exists (stale)' if installed else 'absent'}")
        _say(f"  copying {src} -> {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if installed:
            # copytree(dirs_exist_ok=True) never DELETES, so a file this
            # version dropped would linger forever -- and `_dirs_identical`
            # counts it, so every later run would report "stale" and re-copy.
            # Replacing the tree is what makes the idempotence claim true.
            shutil.rmtree(dest)
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
        _say(f"  cannot enable: {_gsettings_why(SHELL_SCHEMA, ENABLED_KEY)}.")
    else:
        # Re-read, because the `gnome-extensions enable` above may itself have
        # added the uuid -- but NEVER fall back to "[]" when that read fails.
        # This was `_gsettings_get(...) or "[]"`, which turned "could not read
        # the list" into "the list is empty" and wrote that back, deleting
        # every enabled extension on the machine: observed 2026-09-01 on a box
        # where fourteen extensions became the single one being installed.
        # `enabled_now` is the last value a read is known to have returned --
        # the guard immediately above proves it is not None.
        current = _gsettings_get(SHELL_SCHEMA, ENABLED_KEY)
        if current is None:
            current = enabled_now
        new = list_with_uuid(current, EXTENSION_UUID)
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
        _say("  extension is already live on D-Bus (com.zeticle.deskwright) --"
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


def server_command() -> tuple[str, str | None]:
    """How to invoke the MCP server on this machine, and any caveat.

    Three routes, in the order they should be preferred: the console script on
    PATH; the console script beside this interpreter (a pipx or venv install
    whose bin directory the user has not added to PATH -- printing the bare
    name there hands the MCP client an ENOENT); the checkout's
    `mcp_server.py`, which is what a clone has before anything is installed.
    """
    found = _which("deskwright")
    if found:
        return found, None
    beside = Path(sys.executable).with_name("deskwright")
    if beside.is_file():
        return str(beside), (f"{beside.parent} is not on your PATH -- the"
                             " absolute path above works regardless, and"
                             " `pipx ensurepath` fixes the PATH itself.")
    local = Path(__file__).resolve().parent.parent / "mcp_server.py"
    if local.is_file():
        return str(local), None
    return "deskwright", ("not found on PATH, beside this"
                                    " interpreter, or in a checkout -- install"
                                    " the package before registering it.")


def step_mcp() -> None:
    _header("Claude Code registration (printed, not run)")
    cmd, caveat = server_command()
    _say(f'  claude mcp add deskwright --scope user -- "{cmd}"')
    if not cmd.endswith("mcp_server.py"):
        _say("  (`deskwright` is the console script this package"
             " installs; it needs no checkout.)")
    if caveat:
        _say(f"  NOTE: {caveat}")
    _say("  Auto-approval allowlist and the reasoning behind it:")
    _say("  https://github.com/tristanmuzzu/deskwright"
         "/blob/main/docs/claude-code-setup.md")


def step_self_test() -> None:
    _header("proving it works")
    cmd, _ = server_command()
    _say(f"  {cmd} --self-test")
    _say(f"  DESKWRIGHT_SESSION=headless {cmd} --self-test"
         "   # ...or on a desktop you cannot see")
    _say("  Not run by this script: the self-test injects input (it probes the"
         " key-combo guards),")
    _say("  so run it yourself when you are looking at the screen.")


# ---------------------------------------------------------------- main

def _desktop_seam() -> tuple[str, str]:
    """(XDG_CURRENT_DESKTOP, XDG_SESSION_TYPE) -- a seam so tests can set it."""
    return (os.environ.get("XDG_CURRENT_DESKTOP", ""),
            os.environ.get("XDG_SESSION_TYPE", ""))


def check_desktop() -> str | None:
    """Why this machine is not a target, or None when it is.

    Without this, `deskwright-setup` on KDE or Hyprland set a GNOME gsettings key,
    created a `~/.local/share/gnome-shell/extensions` tree on a machine with
    no gnome-shell, demanded a logout, and exited 0. The support matrix says
    GNOME-only; the tool has to say it too, BEFORE it changes anything.
    """
    current, session = _desktop_seam()
    names = [n for n in current.split(":") if n]
    if names and not any(n.upper() == "GNOME" for n in names):
        return (f"XDG_CURRENT_DESKTOP is {current!r}, not GNOME. This project "
                "drives GNOME Shell through its own extension and "
                "org.gnome.Mutter.RemoteDesktop; KDE and wlroots are on the "
                "roadmap through xdg-desktop-portal, but the window verbs, "
                "screenshots and the halt switch are not there yet.")
    if session and session.lower() == "x11":
        return ("XDG_SESSION_TYPE is x11. This is a Wayland project by "
                "design -- on X11, xdotool and wmctrl already do this well.")
    return None


def run(check_only: bool, repo_arg: str | None, force: bool = False) -> int:
    family = detect_host_family()
    src = find_extension_source(repo_arg)
    _say("deskwright-setup"
         + (" --check (read-only, changes nothing)" if check_only else "")
         + f" -- distro family: {family}"
         + (f" -- extension source: {src}" if src
            else " -- extension source: NOT FOUND"))

    wrong_desktop = None if force else check_desktop()
    if wrong_desktop:
        _header("this machine is not a target")
        _say(f"  {wrong_desktop}")
        _say("  Nothing was changed. Support matrix:")
        _say("  https://github.com/tristanmuzzu/deskwright"
             "#support-matrix")
        _say()
        _say("  Re-run with --force if you are setting up for a GNOME session"
             " you are not currently logged into.")
        return 2

    _header("dependencies")
    lines, missing_hard = probe_deps(family)
    for line in lines:
        _say(line)

    step_accessibility(check_only)
    step_extension(src, check_only)
    step_ydotoold()
    step_mcp()
    step_self_test()

    _say()
    if missing_hard:
        _say(f"RESULT: {len(missing_hard)} hard requirement(s) missing --"
             " install them (lines above), then re-run deskwright-setup.")
        return 1
    if src is None:
        # Silently exiting 0 here told the user everything was fine while the
        # compositor half was never installed -- the failure would only show
        # up as missing window verbs after a logout.
        _say("RESULT: dependencies are fine, but the gnome-shell extension"
             " source was not found, so nothing was installed."
             + (f" --repo {repo_arg!r} does not contain"
                f" extension/{EXTENSION_UUID}." if repo_arg else
                " Reinstall the package: the bundled copy is missing."))
        return 1
    _say("RESULT: all hard requirements present."
         + ("" if check_only else " Setup steps applied (idempotent -- safe"
            " to run again)."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deskwright-setup",
        description="Set up deskwright on a fresh machine. Never"
                    " sudos; root-needing steps are printed, not run."
                    " Safe to run repeatedly.")
    parser.add_argument("--check", action="store_true",
                        help="read-only: detect everything, change nothing,"
                             " exit nonzero if a hard requirement is missing")
    parser.add_argument("--force", action="store_true",
                        help="set up even though this does not look like a"
                             " GNOME Wayland session (for preparing a machine"
                             " you are not logged into yet)")
    parser.add_argument("--repo", metavar="DIR", default=None,
                        help="install the gnome-shell extension from this"
                             " checkout (or extension directory) instead of"
                             " the copy bundled with this package")
    args = parser.parse_args(argv)
    return run(check_only=args.check, repo_arg=args.repo, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
