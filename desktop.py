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
    ./desktop.py windows
    ./desktop.py activate <id>
    ./desktop.py screenshot out.png [--cursor]
    ./desktop.py type "hello world"
    ./desktop.py key ctrl+s
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

BUS_NAME = "org.tristan.MigrationHelpers"
OBJ_PATH = "/org/tristan/MigrationHelpers"
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

MODIFIERS = {"ctrl", "control", "leftctrl", "shift", "leftshift", "alt",
             "leftalt", "super", "meta", "win"}


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(msg, file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------- D-Bus

def call(method: str, *args: str) -> str:
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
                "the Migration Helpers extension is not running.\n"
                "  gnome-extensions info migration-helpers@tristan.local\n"
                "If it was just installed or changed, the shell only picks that up\n"
                "at session start — log out and back in."
            )
        die(err)
    return result.stdout.strip()


def cmd_windows(args):
    raw = call("ListWindows")
    # gdbus prints ('<json>',) with the JSON escaped as a GVariant string.
    match = re.match(r"^\('(.*)',\)$", raw, re.S)
    if not match:
        die(f"unexpected reply: {raw}")
    payload = match.group(1).encode().decode("unicode_escape")
    windows = json.loads(payload)
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
