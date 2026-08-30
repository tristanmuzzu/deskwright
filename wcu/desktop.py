#!/usr/bin/env python3
"""Drive this GNOME/Wayland desktop from a script.

Three capabilities, three different mechanisms, because Wayland gives no single
one of them to a client:

  screenshot / windows / activate -> the Migration Helpers GNOME Shell extension
      over D-Bus. gnome-shell refuses these to ordinary clients
      ("Screenshot is not allowed"), but an extension runs inside the shell.

  type / key -> ydotool, injecting through /dev/uinput below the compositor.
      This is focus-blind: it types wherever focus happens to be, which is why
      `activate` exists and should be used first.

  everything semantic (finding and pressing a specific widget) -> AT-SPI, in
      atspi_ui.py. Prefer it. Pressing the real button cannot miss.

Usage:
    python3 -m wcu.desktop windows
    python3 -m wcu.desktop activate <id>
    python3 -m wcu.desktop screenshot out.png [--cursor]
    python3 -m wcu.desktop type "hello world"
    python3 -m wcu.desktop key ctrl+s
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys

# The bundled extension owns org.wcu.Helpers. The migration-helpers names are
# the predecessor this grew out of and are kept only as a fallback, so a
# machine still running the old extension is not stranded -- but the shipped
# one has to be tried FIRST, or this CLI looks broken on every fresh install.
NEW_BUS = "org.wcu.Helpers"
NEW_PATH = "/org/wcu/Helpers"
NEW_UUID = "wcu@wayland-computer-use"
OLD_BUS = "org.tristan.MigrationHelpers"
OLD_PATH = "/org/tristan/MigrationHelpers"
OLD_UUID = "migration-helpers@tristan.local"

BUS_NAME, OBJ_PATH, EXTENSION_UUID = NEW_BUS, NEW_PATH, NEW_UUID
_BUS_PROBED = False
YDOTOOL_SOCKET = "/run/ydotoold.socket"

# Linux evdev keycodes for the keys we can name.
KEYS = {
    "ctrl": 29, "control": 29, "leftctrl": 29,
    "shift": 42, "leftshift": 42,
    "alt": 56, "leftalt": 56,
    "super": 125, "meta": 125, "win": 125,
    "enter": 28, "return": 28, "esc": 1, "escape": 1,
    "tab": 15, "backspace": 14, "delete": 111, "space": 57,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "insert": 110,
}
KEYS.update({chr(c): 30 + i for i, c in enumerate(range(ord("a"), ord("z") + 1))})
# The letter block above is only correct for the QWERTY home ordering, so fix
# the ones that are not sequential.
KEYS.update({
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
})
KEYS.update({f"f{n}": code for n, code in enumerate(range(59, 71), start=1)})
KEYS.update({"f11": 87, "f12": 88})
# Digits and the punctuation an application is most likely to bind. Their
# absence meant press_keys could not send `ctrl+1` to switch a tab or `7` to a
# calculator, and the error listed only the letters, as though digits were exotic.
KEYS.update({"1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
             "6": 7, "7": 8, "8": 9, "9": 10, "0": 11})
#
# The names here must match remote_input.KEYSYMS exactly. parse_combo is the
# gatekeeper and reads this table; what actually gets injected is the keysym. A
# name in one table and not the other is accepted and then refused, which reads
# as a bug in the compositor. tests/test_key_tables.py holds them level.
KEYS.update({"minus": 12, "equal": 13, "comma": 51, "period": 52,
             "slash": 53, "semicolon": 39, "apostrophe": 40, "grave": 41,
             "backslash": 43, "bracketleft": 26, "bracketright": 27,
             "plus": 78,                      # keypad plus; the only bare one
             "capslock": 58, "numlock": 69, "scrolllock": 70,
             "print": 99, "pause": 119, "menu": 127,
             "rightctrl": 97, "rightshift": 54, "rightalt": 100, "altgr": 100})

MODIFIERS = {"ctrl", "control", "leftctrl", "shift", "leftshift", "alt",
             "leftalt", "super", "meta", "win"}


def die(msg: str) -> NoReturn:  # noqa: F821
    print(msg, file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------- D-Bus

def _pick_bus() -> None:
    """Prefer the bundled extension's bus; fall back to migration-helpers.

    Mirrors `wcu/shell.py::_pick_bus`; this CLI predates it and used to
    hardcode the old name, so it failed on every machine but its author's.
    """
    global BUS_NAME, OBJ_PATH, EXTENSION_UUID, _BUS_PROBED
    if _BUS_PROBED:
        return
    _BUS_PROBED = True
    probe = subprocess.run(
        ["gdbus", "introspect", "--session", "--dest", NEW_BUS,
         "--object-path", NEW_PATH, "--xml"],
        capture_output=True, text=True)
    if probe.returncode != 0 or "<method" not in probe.stdout:
        BUS_NAME, OBJ_PATH, EXTENSION_UUID = OLD_BUS, OLD_PATH, OLD_UUID


def call(method: str, *args: str) -> str:
    _pick_bus()
    cmd = [
        "gdbus", "call", "--session", "--dest", BUS_NAME,
        "--object-path", OBJ_PATH,
        "--method", f"{BUS_NAME}.{method}", *args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip()
        if "ServiceUnknown" in err or "was not provided" in err:
            die(
                f"the {EXTENSION_UUID} extension is not running.\n"
                f"  gnome-extensions info {EXTENSION_UUID}\n"
                "If it was just installed or changed, the shell only picks that up\n"
                "at session start — log out and back in."
            )
        die(err)
    return result.stdout.strip()


def cmd_windows(args):
    raw = call("ListWindows")
    # gdbus prints ('<json>',) and switches to ("<json>",) the moment the payload
    # contains an apostrophe -- which any window title can, and the JSON payload
    # itself always does once a title is quoted. literal_eval accepts both
    # quotings and undoes the escapes; the old regex matched only the
    # single-quoted form and died on the other, and unicode_escape mangled
    # non-ASCII titles. mcp_server.py fixed this in _unwrap_gvariant_string;
    # this copy did not (found 2026-08-13).
    try:
        value = ast.literal_eval(raw)
        if not (isinstance(value, tuple) and len(value) == 1 and isinstance(value[0], str)):
            raise ValueError("not a single-string reply")
        windows = json.loads(value[0])
    except (SyntaxError, ValueError) as exc:
        die(f"unexpected reply: {raw[:200]} ({exc})")
    if args.json:
        print(json.dumps(windows, indent=2))
        return
    for w in windows:
        flags = " ".join(
            f for f, on in (
                ("focused", w["focused"]),
                ("minimized", w["minimized"]),
                ("above", w["above"]),
            ) if on
        )
        print(
            f'{w["id"]:>10}  {w["wm_class"] or "?":<24} '
            f'{w["width"]}x{w["height"]}+{w["x"]}+{w["y"]:<6} '
            f'{flags:<20} {w["title"]!r}'
        )


def cmd_activate(args):
    print(call("ActivateWindow", str(args.id)))


def cmd_screenshot(args):
    path = os.path.abspath(args.path)
    reply = call("Screenshot", path, "true" if args.cursor else "false")
    print(reply)
    if not os.path.exists(path):
        die("call returned but no file was written")
    print(f"{path}: {os.path.getsize(path)} bytes")


# --------------------------------------------------------------------- input

def ydotool(*args: str) -> None:
    if not shutil.which("ydotool"):
        die("ydotool is not installed")
    if not os.path.exists(YDOTOOL_SOCKET):
        die(f"{YDOTOOL_SOCKET} missing — is ydotoold running? (systemctl status ydotoold)")
    env = dict(os.environ, YDOTOOL_SOCKET=YDOTOOL_SOCKET)
    subprocess.run(["ydotool", *args], env=env, check=True)


def parse_combo(combo: str) -> list[int]:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    codes = []
    for part in parts:
        if part not in KEYS:
            die(f"unknown key {part!r}")
        codes.append(KEYS[part])

    # Refuse the one combination that does not do what it looks like it does.
    # Ctrl+Alt+F1..F12 is switch-to-session in mutter: it throws the desktop
    # onto a different virtual terminal showing a login screen, which is
    # indistinguishable from a frozen machine. Injecting one of these cost a
    # session and a hard power-off on 2026-08-08.
    mods = {p for p in parts if p in MODIFIERS}
    fkeys = {p for p in parts if re.fullmatch(r"f([1-9]|1[0-2])", p)}
    if fkeys and {"ctrl", "control", "leftctrl"} & mods and {"alt", "leftalt"} & mods:
        die(
            f"refusing to inject {combo!r}: Ctrl+Alt+F1-F12 switches virtual "
            "terminal and will look exactly like the machine has frozen."
        )
    return codes


def cmd_key(args):
    codes = parse_combo(args.combo)
    sequence = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    ydotool("key", "--key-delay", "40", *sequence)
    print(f"sent {args.combo}")


def cmd_type(args):
    ydotool("type", "--key-delay", str(args.delay), args.text)
    print(f"typed {len(args.text)} characters")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("windows", help="list windows with geometry and focus")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("activate", help="focus and raise a window by id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_activate)

    p = sub.add_parser("screenshot")
    p.add_argument("path")
    p.add_argument("--cursor", action="store_true")
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("type", help="type text wherever focus currently is")
    p.add_argument("text")
    p.add_argument("--delay", type=int, default=20)
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help="send a key combination, e.g. ctrl+shift+t")
    p.add_argument("combo")
    p.set_defaults(func=cmd_key)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
