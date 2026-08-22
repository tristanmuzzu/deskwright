#!/usr/bin/env python3
"""MCP server for driving this GNOME/Wayland desktop.

The point of this file is that "hey Claude, do this on my laptop" should work in
every session without ceremony -- no remembering a script path, no shell quoting,
no separate setup step.

WHAT IT IS FOR AND WHAT IT REFUSES

Four mechanisms, because Wayland hands a client none of them directly:

  * gnome-shell extension over D-Bus (`org.tristan.MigrationHelpers`) for
    screenshots, window geometry, and focus. gnome-shell refuses
    `org.gnome.Shell.Screenshot` and `GrabAccelerator` to ordinary clients, and
    `grim` is wlroots-only, so an extension running inside the shell is the only
    path that exists on GNOME.
  * AT-SPI for anything semantic. `ui_find` then `ui_press` presses the real
    widget, so it cannot miss, cannot be defeated by a window moving, and needs
    no pointer at all. **Prefer this over typing and key combos.**
  * `org.gnome.Mutter.RemoteDesktop` for the pointer and for text: absolute
    motion in screen coordinates, real buttons and wheel, and keysyms, which
    are characters rather than key positions and so cannot be transposed by the
    keyboard layout. See remote_input.py for why this replaced ydotool.
  * ydotool through /dev/uinput, now only a fallback. It cannot point (its
    absolute mode is dead here and relative motion goes through pointer
    acceleration) and its keycodes are mapped through the active XKB layout,
    which on this machine's `de` layout turns every typed `y` into a `z`.

WHERE THINGS ARE, IN PIXELS

`screen_map` answers "where do I click for X" without an image: every window
top of the stack first with its centre, and every pressable widget of the
focused application with the exact point to click it at. `window_at` answers
"what would a click here hit" before the click happens, and `pointer_click`
takes `expect_window` so a click that would land somewhere else is refused
rather than delivered. `screenshot annotate` draws the same information onto
the picture, labelled in screen coordinates.

TWO THINGS THIS SERVER DOES THAT THE CLI DID NOT

1. Focus is proven, not assumed. Every tool that injects input takes a `target`
   window, activates it, and then polls `ListWindows` until that window actually
   reports `focused: true`. If focus never lands, the tool returns an error and
   types nothing. Focus-blind injection into the wrong window is the single
   easiest way to do real damage on this machine.
2. Widget identity is re-verified before acting. An AT-SPI index path is only
   valid while the tree is unchanged, so `ui_press` requires the caller to state
   the name or role it expects and refuses to act if the resolved widget no
   longer matches.

It also refuses Ctrl+Alt+F1-F12 outright. That is switch-to-session in mutter: it
throws the desktop onto a VT login screen, which is indistinguishable from a
frozen machine. An agent did this on 2026-08-08, could not observe the result,
kept typing into a password box, and cost a hard power-off.

Transport is hand-rolled JSON-RPC over stdio on purpose: no third-party import
can drift or vanish out from under a server whose whole job is to be reliable.

    ./mcp_server.py                # speak MCP on stdio (how Claude Code runs it)
    ./mcp_server.py --self-test    # prove every capability, print a report, exit
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

BUS_NAME = "org.tristan.MigrationHelpers"
OBJ_PATH = "/org/tristan/MigrationHelpers"
YDOTOOL_SOCKET = "/run/ydotoold.socket"
EXTENSION_UUID = "migration-helpers@tristan.local"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "wayland-computer-use", "version": "1.0.0"}

FOCUS_TIMEOUT_S = 3.0
FOCUS_POLL_S = 0.1
MAX_TREE_NODES = 400          # keeps a tree dump inside a sane token budget
DEFAULT_TREE_DEPTH = 8
# GTK4 nests brutally: gnome-text-editor's document text view sits at depth 23
# behind a stack of anonymous panels and groupings. A depth-8 search finds
# nothing at all in a modern GNOME app, which reads as "the app has no widgets"
# rather than "you did not look far enough". Search deep and cap on node count.
DEFAULT_FIND_DEPTH = 30
MAX_FIND_NODES = 4000

# Evdev keycodes. Imported from desktop.py so there is one table, not two.
try:
    from desktop import KEYS, MODIFIERS  # type: ignore
except Exception:  # pragma: no cover - desktop.py sits next to this file
    KEYS, MODIFIERS = {}, set()


class ToolError(Exception):
    """A failure the model should see and can act on, not a crash."""


# =========================================================================
# transport-independent capability layer
# =========================================================================
def _gdbus(method: str, *args: str, timeout: float = 30.0) -> str:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise ToolError(
            "DBUS_SESSION_BUS_ADDRESS is not set, so the session bus is "
            "unreachable. This server has to run inside Tristan's graphical "
            "session -- it cannot work over a bare ssh login or from a system "
            "service."
        )
    cmd = ["gdbus", "call", "--session", "--dest", BUS_NAME,
           "--object-path", OBJ_PATH, "--method", f"{BUS_NAME}.{method}", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{method} did not answer within {timeout:.0f}s") from None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "ServiceUnknown" in err or "was not provided" in err or "NoReply" in err:
            raise ToolError(
                f"the {EXTENSION_UUID} extension is not answering on D-Bus.\n"
                f"  {_extension_diagnosis()}"
            )
        raise ToolError(err or f"{method} failed with rc={proc.returncode}")
    return proc.stdout.strip()


def _extension_state() -> str:
    try:
        out = subprocess.run(["gnome-extensions", "info", EXTENSION_UUID],
                             capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"State:\s*(\S+)", out)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _extension_diagnosis() -> str:
    """Say WHICH of the two very different causes this is.

    They look identical from a failed D-Bus call and have opposite remedies, and
    a session that guesses wrong burns an hour. INACTIVE-while-enabled usually
    means the screen is locked: gnome-shell unloads every extension whose
    metadata.json does not list "unlock-dialog" in session-modes.

    Which this one lists is read from the file rather than remembered here --
    it was changed to ["user", "unlock-dialog"] after this comment first
    claimed otherwise, and a hardcoded answer would now be confidently wrong.
    """
    state = _extension_state()
    if state == "INACTIVE":
        modes = []
        try:
            meta = Path.home() / ".local/share/gnome-shell/extensions" / EXTENSION_UUID / "metadata.json"
            modes = json.loads(meta.read_text()).get("session-modes") or []
        except Exception:
            pass
        if "unlock-dialog" not in modes:
            return (
                "State is INACTIVE and its metadata.json session-modes is "
                f"{modes or ['user']}, so THE SCREEN IS ALMOST CERTAINLY LOCKED -- "
                "gnome-shell unloads extensions that do not declare "
                '"unlock-dialog". Nothing is broken. Screenshots and window '
                "geometry come back on unlock. AT-SPI (ui_find / ui_press / "
                "ui_tree) keeps working while locked, so prefer those. To keep "
                "this working while locked, Tristan has to add "
                '"unlock-dialog" to session-modes and log out once -- that is his '
                "call, because it also makes screenshots possible while locked."
            )
        return ("State is INACTIVE despite declaring unlock-dialog -- the extension "
                "itself failed to load. Check journalctl --user -b for its error.")
    if state in ("ERROR", "OUT_OF_DATE"):
        return (f"State is {state}: the extension is installed but broken. "
                "journalctl --user -b will have the stack trace.")
    return (f"State is {state}. gnome-shell only scans extensions at session start "
            "and Wayland offers no way to restart the shell, so if the extension "
            "was just installed or changed this needs a logout -- retrying will "
            "not help.")


def _unwrap_gvariant_string(raw: str) -> str:
    """gdbus prints ('<payload>',), but switches to ("<payload>",) the moment the
    payload contains an apostrophe -- which any window title can. literal_eval
    accepts both quotings and undoes the escapes; the old regex + unicode_escape
    rejected the double-quoted form outright and mangled non-ASCII titles."""
    try:
        value = ast.literal_eval(raw.strip())
    except (SyntaxError, ValueError) as exc:
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}") from exc
    if not (isinstance(value, tuple) and len(value) == 1 and isinstance(value[0], str)):
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}")
    return value[0]


def list_windows() -> list[dict]:
    """Every window, bottom of the stack first -- the order gnome-shell keeps
    them in, which is what makes the last match at a point the topmost one."""
    return json.loads(_unwrap_gvariant_string(_gdbus("ListWindows")))


# ---- what the running extension can actually do ---------------------------
_EXTENSION_METHODS: set[str] | None = None


def extension_methods() -> set[str]:
    """Which methods the LOADED extension has, which is not the same as the
    methods in its source.

    gnome-shell imports an extension once per session and cannot reload it on
    Wayland -- `ReloadExtension` answers "deprecated and does not work" on 50.1,
    and disable/enable re-runs enable() against the already-imported module. So
    a file edited an hour ago is not running until the next login, and a client
    that assumes otherwise fails with UnknownMethod and no explanation.
    """
    global _EXTENSION_METHODS
    if _EXTENSION_METHODS is None:
        try:
            xml = subprocess.run(
                ["gdbus", "introspect", "--session", "--dest", BUS_NAME,
                 "--object-path", OBJ_PATH, "--xml"],
                capture_output=True, text=True, timeout=15).stdout
            _EXTENSION_METHODS = set(re.findall(r'<method name="([^"]+)"', xml))
        except Exception:
            _EXTENSION_METHODS = set()
    return _EXTENSION_METHODS


def _needs_relogin(method: str) -> str:
    return (
        f"the running gnome-shell extension has no {method} method. The source "
        f"in ~/.local/share/gnome-shell/extensions/{EXTENSION_UUID}/ may already "
        "have it: gnome-shell only imports extensions at session start and "
        "Wayland has no way to restart it, so this needs a log out and back in. "
        "Everything that does not depend on it keeps working."
    )


def _resolve_target(target: Any) -> dict:
    """Turn a window id, wm_class, or title fragment into exactly one window."""
    windows = list_windows()
    if not windows:
        raise ToolError("no windows are open, so there is nothing to target")

    if isinstance(target, bool):
        raise ToolError("target must be a window id or a name, not a boolean")

    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        wanted = int(target)
        for w in windows:
            if w["id"] == wanted:
                return w
        raise ToolError(
            f"no window with id {wanted}. Window ids are not stable: a dialog "
            "that is closed and reopened -- a file chooser, a save prompt -- comes "
            "back with a new one, so an id read a few calls ago can already be "
            "dead. Pass a wm_class or a title fragment instead, which survives it. "
            "Open windows: "
            + ", ".join(f'{w["id"]} {w["wm_class"]!r} {w["title"]!r}' for w in windows)
        )

    if not isinstance(target, str) or not target.strip():
        raise ToolError("target must be a window id, a wm_class, or a title fragment")

    needle = target.strip().lower()
    exact = [w for w in windows if (w["wm_class"] or "").lower() == needle]
    if len(exact) == 1:
        return exact[0]
    matches = exact or [
        w for w in windows
        if needle in (w["wm_class"] or "").lower() or needle in (w["title"] or "").lower()
    ]
    if not matches:
        raise ToolError(
            f"nothing matches {target!r}. Open windows: "
            + ", ".join(f'{w["id"]} {w["wm_class"]!r} {w["title"]!r}' for w in windows)
        )
    if len(matches) > 1:
        raise ToolError(
            f"{target!r} matches {len(matches)} windows; pass an id instead: "
            + ", ".join(f'{w["id"]} {w["title"]!r}' for w in matches)
        )
    return matches[0]


def focus_window(target: Any) -> dict:
    """Activate a window and PROVE focus landed there before returning.

    ydotool injects below the compositor and has no idea which window is focused,
    so every input tool goes through here first. Returning without proof would
    mean typing into whatever happened to be in front.
    """
    window = _resolve_target(target)
    wid = window["id"]

    if window.get("focused"):
        return {"window": window, "activated": False,
                "detail": f'already focused: {window["wm_class"]} {window["title"]!r}'}

    _gdbus("ActivateWindow", str(wid))

    deadline = time.monotonic() + FOCUS_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        time.sleep(FOCUS_POLL_S)
        for w in list_windows():
            if w["id"] == wid:
                last = w
                if w.get("focused"):
                    return {"window": w, "activated": True,
                            "detail": f'focus confirmed on {w["wm_class"]} {w["title"]!r}'}
                break
    state = "minimized" if (last or {}).get("minimized") else "not focused"
    raise ToolError(
        f'activated window {wid} ({window["wm_class"]}) but it is still {state} after '
        f"{FOCUS_TIMEOUT_S:.0f}s. Nothing was typed. A window that refuses focus is "
        "usually minimized on another workspace, or a modal dialog owns the focus."
    )


_LAYOUT_CACHE: list[str] | None = None


def keyboard_layouts() -> list[str]:
    """Active XKB layouts. Cached; the layout does not change mid-session."""
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is None:
        try:
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
                capture_output=True, text=True, timeout=15).stdout
            _LAYOUT_CACHE = re.findall(r"'xkb',\s*'([^']+)'", out) or []
        except Exception:
            _LAYOUT_CACHE = []
    return _LAYOUT_CACHE


def layout_hazard() -> str:
    """Why injected keystrokes may arrive as different characters.

    ydotool writes raw evdev keycodes into /dev/uinput, BELOW the compositor. The
    compositor then maps those keycodes through the active XKB layout. ydotool's
    character-to-keycode table assumes US QWERTY, so on any other layout the
    characters that moved arrive transposed.

    Measured on this machine 2026-08-08: layout is `de` (QWERTZ), and
    `ydotool type "ydo1"` landed as "zdo1" -- y and z are swapped on QWERTZ. Most
    punctuation moves too. This is silent: ydotool exits 0 either way.
    """
    layouts = keyboard_layouts()
    if not layouts or layouts[0] == "us":
        return ""
    return (f"keyboard layout is {layouts[0]!r}, not 'us'. ydotool injects raw "
            "US-QWERTY keycodes below the compositor, which then maps them through "
            f"the {layouts[0]!r} layout, so characters that differ between the two "
            "arrive TRANSPOSED (on de, y<->z, and most punctuation moves). Prefer "
            "ui_set_text, which passes characters to the widget and is unaffected.")


def _ydotool(*args: str, timeout: float = 30.0) -> None:
    if not shutil.which("ydotool"):
        raise ToolError("ydotool is not installed, so no input can be injected")
    if not os.path.exists(YDOTOOL_SOCKET):
        raise ToolError(
            f"{YDOTOOL_SOCKET} is missing -- ydotoold is not running. It must be a "
            "SYSTEM service (systemctl status ydotoold); a user service cannot open "
            "/dev/uinput when the session predates the groupadd."
        )
    env = dict(os.environ, YDOTOOL_SOCKET=YDOTOOL_SOCKET)
    try:
        proc = subprocess.run(["ydotool", *args], env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"ydotool did not finish within {timeout:.0f}s") from None
    if proc.returncode != 0:
        raise ToolError(f"ydotool failed: {(proc.stderr or '').strip()[:200]}")


def parse_combo(combo: str) -> list[int]:
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ToolError("empty key combination")

    mods = {p for p in parts if p in MODIFIERS}
    has_ctrl = bool({"ctrl", "control", "leftctrl"} & mods)
    has_alt = bool({"alt", "leftalt"} & mods)
    fkeys = {p for p in parts if re.fullmatch(r"f([1-9]|1[0-2])", p)}
    if fkeys and has_ctrl and has_alt:
        raise ToolError(
            f"refusing to inject {combo!r}. Ctrl+Alt+F1-F12 is switch-to-session in "
            "mutter: it throws the desktop onto a VT login screen that is "
            "indistinguishable from a frozen machine, and subsequent keystrokes go "
            "into a password box. This happened on 2026-08-08 and cost a hard "
            "power-off. There is no flag to override this."
        )
    # Same failure class, different key: Ctrl+Alt+Delete is bound to `logout` on
    # this machine (verified via org.gnome.settings-daemon.plugins.media-keys).
    # It tears the session down, takes unsaved work with it, and an injecting
    # client cannot observe that it happened.
    if has_ctrl and has_alt and ({"delete", "backspace"} & set(parts)):
        raise ToolError(
            f"refusing to inject {combo!r}. Ctrl+Alt+Delete is bound to logout here: "
            "it ends the session, discards unsaved work, and leaves an injecting "
            "client with no way to observe that anything happened. There is no flag "
            "to override this."
        )
    codes = []
    for part in parts:
        if part not in KEYS:
            raise ToolError(
                f"unknown key {part!r}. Known: {', '.join(sorted(KEYS)[:24])}, ..."
            )
        codes.append(KEYS[part])
    return codes


def combo_keysyms(combo: str) -> list[int]:
    """The same combination as layout-independent keysyms.

    parse_combo stays the gatekeeper -- it holds the refusals, and it runs
    first -- but what actually gets injected is this, because a keycode is a
    position on a US keyboard and a keysym is the key that was meant.
    """
    parse_combo(combo)                  # refusals and unknown-key errors first
    import remote_input
    syms = []
    for part in [p.strip().lower() for p in str(combo).split("+") if p.strip()]:
        sym = remote_input.KEYSYMS.get(part)
        if sym is None:
            raise ToolError(f"no keysym known for {part!r}")
        syms.append(sym)
    return syms


# ---- pointer, in the coordinates list_windows already speaks --------------
def _input():
    """The mutter RemoteDesktop input layer, imported late.

    Late because it needs PyGObject and a session bus, and a machine missing
    either should still get windows, screenshots and AT-SPI rather than a server
    that will not start.
    """
    try:
        import remote_input
    except Exception as e:                                  # pragma: no cover
        raise ToolError(
            f"the pointer layer is unavailable ({type(e).__name__}: {e}). "
            "It needs PyGObject (python3-gi) and a session bus."
        ) from None
    return remote_input


class _InputProxy:
    """The input session with its failures re-raised as ToolError.

    remote_input keeps its own exception type because it is useful on its own,
    but anything that reaches the model has to be a ToolError: an InputError
    escaping a handler surfaces as an internal error with no advice in it,
    which is exactly the shape of message this server exists to avoid.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if type(e).__name__ == "InputError":
                    raise ToolError(str(e)) from None
                raise
        return wrapped


def _pointer() -> Any:
    return _InputProxy(_input().shared())


def _point(a: dict, xk: str = "x", yk: str = "y") -> tuple[float, float]:
    for key in (xk, yk):
        if a.get(key) is None:
            raise ToolError(f"{xk} and {yk} are required, in screen pixels")
        if isinstance(a[key], bool) or not isinstance(a[key], (int, float)):
            raise ToolError(f"{key} must be a number, got {a[key]!r}")
    return float(a[xk]), float(a[yk])


def window_at(x: float, y: float) -> dict:
    """Which window a click at this point would be delivered to.

    Two answers, because they can differ and the difference is where clicks get
    lost. `covering` is every window whose rectangle contains the point, top
    first, computed here from the stacking order. `window` is the compositor's
    own answer, picked from the scene graph, which respects input shapes -- a
    click-through overlay covers the rectangle without taking the click. Only
    the newer extension can answer that one; without it this says so rather
    than guessing.
    """
    windows = [w for w in list_windows() if not w.get("minimized")]
    covering = [
        w for w in reversed(windows)                # reversed: topmost first
        if w["x"] <= x < w["x"] + w["width"] and w["y"] <= y < w["y"] + w["height"]
    ]
    result: dict[str, Any] = {"x": x, "y": y, "covering": covering}

    if "WindowAt" in extension_methods():
        picked = json.loads(_unwrap_gvariant_string(
            _gdbus("WindowAt", str(int(x)), str(int(y)))))
        result["window"] = picked.get("window")
        result["source"] = "compositor pick (input shapes respected)"
    else:
        result["window"] = covering[0] if covering else None
        result["source"] = "window rectangles and stacking order"
        result["caveat"] = (
            "the compositor was not asked, so an input-shaped or click-through "
            "window (an overlay, a HUD) will be reported as the target even "
            "though the click falls through it. " + _needs_relogin("WindowAt")
        )
    return result


def pointer_position() -> dict:
    """Where the pointer is.

    The extension knows exactly. Without it, the only honest answer is where
    this server last put the pointer, clearly labelled as such, because X's
    answer through XWayland is stale the moment the pointer is over a Wayland
    surface and looks perfectly plausible while being wrong.
    """
    if "Pointer" in extension_methods():
        data = json.loads(_unwrap_gvariant_string(_gdbus("Pointer")))
        return {"x": data["x"], "y": data["y"], "source": "compositor"}
    ri = _input().shared()
    if ri.last_position:
        return {
            "x": ri.last_position[0], "y": ri.last_position[1],
            "source": "last position this server set",
            "age_seconds": round(time.time() - ri.last_position_at, 1),
            "caveat": "a hand on the mouse since then is invisible here. "
                      + _needs_relogin("Pointer"),
        }
    raise ToolError(
        "nothing knows where the pointer is: this server has not moved it, and "
        + _needs_relogin("Pointer")
    )


def _guard_point(x: float, y: float, expect: Any) -> dict:
    """Refuse to click when the thing under the point is not what was expected.

    This exists because of three real misdirected clicks on 2026-08-16: an
    agent aiming at a small window under a mis-measured pointer hit the browser
    behind it instead, knocked a video out of full screen, and opened a system
    permission dialog. A click that names the window it believes it is aiming
    at can be refused instead of landing somewhere else.
    """
    wanted = _resolve_target(expect)
    at = window_at(x, y)
    target = at.get("window")
    if target and target.get("id") == wanted["id"]:
        return {"expected": wanted["wm_class"], "confirmed_by": at["source"]}
    covering_ids = [w["id"] for w in at["covering"]]
    if target is None and wanted["id"] in covering_ids:
        # Rect says yes, the compositor says nothing is there: the point is over
        # a hole in an input shape, or over the desktop.
        raise ToolError(
            f"({x:.0f}, {y:.0f}) is inside {wanted['wm_class']}'s rectangle but the "
            "compositor routes no click there -- the window is click-through at "
            "that point. Nothing was clicked."
        )
    # Include the id. Two windows of the same application read as
    # "expected org.gnome.Calculator but org.gnome.Calculator is there", which
    # looks like the guard malfunctioning rather than a second instance.
    found = (f'{target["wm_class"]} (id {target.get("id")}) {target.get("title", "")!r}'
             if target else "nothing (the desktop)")
    raise ToolError(
        f'refusing to click ({x:.0f}, {y:.0f}): expected {wanted["wm_class"]} '
        f'(id {wanted["id"]}) but {found} is there. Nothing was clicked. '
        f"Pass expect_window=null to click anyway, or check screen_map for where "
        f"the window actually is."
    )


# ---- AT-SPI --------------------------------------------------------------
def _atspi():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as e:
        raise ToolError(f"AT-SPI is unavailable ({type(e).__name__}: {e})") from None
    Atspi.init()
    return Atspi


def _describe(node, path: str) -> dict:
    out: dict[str, Any] = {"path": path, "role": node.get_role_name(),
                           "name": node.get_name() or ""}
    try:
        ext = node.get_extents(0)  # 0 == Atspi.CoordType.SCREEN
        if ext.width > 0 and ext.height > 0:
            out["bounds"] = {"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height}
    except Exception:
        pass
    try:
        if node.get_action_iface() and node.get_n_actions() > 0:
            out["actions"] = [node.get_localized_name(i)
                              for i in range(node.get_n_actions())]
    except Exception:
        pass
    return out


def _find_app(app_name: str):
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)
    names = []
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        names.append(app.get_name())
        if app.get_name() == app_name:
            return app
    lowered = app_name.lower()
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is not None and lowered in (app.get_name() or "").lower():
            return app
    raise ToolError(
        f"no application named {app_name!r} on the AT-SPI bus. Present: "
        + ", ".join(repr(n) for n in names)
        + ". An app started while toolkit-accessibility was false exposes a "
          "stunted tree for its whole life -- restart the app, not the setting."
    )


def _walk(node, path: str, depth: int, max_depth: int, out: list[dict],
          cap: int = MAX_TREE_NODES) -> None:
    if len(out) >= cap:
        return
    out.append(_describe(node, path))
    if depth >= max_depth:
        return
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            _walk(child, f"{path}/{i}", depth + 1, max_depth, out, cap)


TEXT_ROLES = ("text", "document_text", "entry", "document frame", "paragraph")


def _text_ifaces(node) -> tuple[Any, Any]:
    """(text_iface, editable_iface) -- either may be None."""
    try:
        text = node.get_text_iface()
    except Exception:
        text = None
    try:
        editable = node.get_editable_text_iface()
    except Exception:
        editable = None
    return text, editable


def _read_text(node) -> str:
    Atspi = _atspi()
    text_iface, _ = _text_ifaces(node)
    if text_iface is None:
        raise ToolError(
            f"{node.get_role_name()} {node.get_name()!r} exposes no AT-SPI text "
            "interface, so its content cannot be read"
        )
    count = Atspi.Text.get_character_count(text_iface)
    return Atspi.Text.get_text(text_iface, 0, count) if count else ""


def _find_text_widget(app_name: str, path: str | None):
    """The editable text widget of an app, or the one at an explicit path."""
    if path:
        return _resolve_path(path)
    app = _find_app(app_name)
    collected: list[dict] = []
    _walk(app, app.get_name(), 0, DEFAULT_FIND_DEPTH, collected, cap=MAX_FIND_NODES)
    candidates = [n for n in collected if n["role"] in TEXT_ROLES]
    if not candidates:
        raise ToolError(
            f"no text widget in {app_name!r} after reading {len(collected)} nodes to "
            f"depth {DEFAULT_FIND_DEPTH}. If the app was started while "
            "toolkit-accessibility was false its tree is stunted for its whole "
            "life -- restart the app."
        )

    # The FOCUSED one first. "The app's first text widget" is the wrong document
    # the moment an editor has two tabs open, which on a real desktop it usually
    # does: typing goes to the visible tab and the readback came from whichever
    # tab happened to be first in the tree, so type_text reported that the wrong
    # characters had arrived when they had arrived perfectly. Documented as a
    # known fragility in tests/test_e2e_real_task.py since 2026-08-17.
    live_candidates = []
    for node in candidates:
        try:
            live = _resolve_path(node["path"])
        except ToolError:
            continue
        _, editable = _text_ifaces(live)
        live_candidates.append((live, editable is not None, _is_focused(live)))

    for want_focus in (True, False):
        for live, editable, focused in live_candidates:
            if editable and focused == want_focus:
                return live
    for live, _editable, focused in live_candidates:
        if focused:
            return live
    if live_candidates:
        return live_candidates[0][0]
    return _resolve_path(candidates[0]["path"])


def _is_focused(node) -> bool:
    """Whether this widget holds the keyboard, according to AT-SPI."""
    try:
        Atspi = _atspi()
        state = node.get_state_set()
        return bool(state.contains(Atspi.StateType.FOCUSED))
    except Exception:
        return False


def _resolve_path(path: str):
    app_name, *indices = str(path).split("/")
    node = _find_app(app_name)
    for part in indices:
        try:
            node = node.get_child_at_index(int(part))
        except (ValueError, TypeError):
            raise ToolError(f"{path!r} is not a valid index path") from None
        if node is None:
            raise ToolError(
                f"{path!r} no longer resolves -- the tree changed since it was found. "
                "Call ui_find again; paths are never cacheable."
            )
    return node


def list_atspi_apps() -> list[dict]:
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)
    apps = []
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is not None:
            apps.append({"name": app.get_name(), "children": app.get_child_count()})
    return apps


# =========================================================================
# tools
# =========================================================================
def tool_list_windows(_: dict) -> dict:
    windows = list_windows()
    return {"count": len(windows), "windows": windows}


SHOT_CACHE = Path(os.path.expanduser("~/.cache/wayland-computer-use/shots"))
SHOT_CACHE_KEEP = 40


def _shot_path(a: dict) -> tuple[Path, bool]:
    """Where the PNG goes. Naming one is now optional, because inventing a
    throwaway /tmp path was pure ceremony on every single call."""
    raw = str(a.get("path") or "").strip()
    if not raw:
        SHOT_CACHE.mkdir(parents=True, exist_ok=True)
        _prune_shot_cache()
        return SHOT_CACHE / f"shot-{time.time():.3f}.png", False

    path = Path(os.path.expanduser(raw)).absolute()
    if path.is_dir():
        raise ToolError(f"{path} is a directory; give a file path ending in .png")
    # The suffix check is load-bearing, not cosmetic: this used to unlink whatever
    # already existed at the caller-supplied path before capturing, so
    # screenshot{"path": "~/system/healthcheck.sh"} deleted that file -- and if the
    # capture then failed (a locked screen is enough), it was simply gone.
    if path.suffix.lower() != ".png":
        raise ToolError(f"path must end in .png, got {path.name!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, True


def _prune_shot_cache() -> None:
    try:
        shots = sorted(SHOT_CACHE.glob("shot-*.png"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-SHOT_CACHE_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:                                       # pragma: no cover
        pass                                                # housekeeping only


def _capture(path: Path, region: tuple[int, int, int, int] | None = None,
             include_cursor: bool = False) -> bool:
    """Put a PNG of `region` (or the whole screen) at `path`.

    Returns whether gnome-shell did the cropping. Capture goes to a temp file
    beside the target and is renamed on success, so an existing file is only
    ever replaced by a real screenshot.
    """
    tmp = path.with_name(f".{path.name}.capturing")
    tmp.unlink(missing_ok=True)
    try:
        if region and "ScreenshotArea" in extension_methods():
            _gdbus("ScreenshotArea", *(str(int(v)) for v in region), str(tmp))
            cropped_in_shell = True
        else:
            _gdbus("Screenshot", str(tmp), "true" if include_cursor else "false")
            cropped_in_shell = False
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ToolError("the call returned but no image was written")
        with tmp.open("rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ToolError("the capture was written but is not a PNG")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return cropped_in_shell


def _occlusion(window: dict) -> dict | None:
    """How much of `window` is covered by windows above it in the stack.

    Captures come from the screen, so a covered window returns whatever is in
    front of it and looks exactly like the target having the wrong content. That
    cost a wasted capture and a wasted Read on 2026-08-20; saying so is free.
    """
    try:
        windows = list_windows()
    except ToolError:
        return None
    seen, above = False, []
    for w in windows:                                       # bottom of stack first
        if w["id"] == window["id"]:
            seen = True
            continue
        if seen and not w.get("minimized"):
            above.append(w)
    if not seen or not above:
        return None

    area = max(1, window["width"] * window["height"])
    covered = 0
    for w in above:
        overlap_w = min(window["x"] + window["width"], w["x"] + w["width"]) - max(window["x"], w["x"])
        overlap_h = min(window["y"] + window["height"], w["y"] + w["height"]) - max(window["y"], w["y"])
        if overlap_w > 0 and overlap_h > 0:
            covered += overlap_w * overlap_h
    if covered <= 0:
        return None
    percent = round(min(100.0, 100.0 * covered / area), 1)
    return {
        "occluded_percent": percent,
        "by": [w["wm_class"] for w in above][:4],
        "detail": (f"{percent}% of this window is behind another one, so the capture "
                   "shows what is in front of it. activate_window first if that is "
                   "not what you wanted."),
    }


def tool_screenshot(a: dict) -> dict:
    path, persistent = _shot_path(a)
    region, window = _screenshot_region(a)

    cropped_in_shell = _capture(path, region, bool(a.get("include_cursor")))

    result: dict[str, Any] = {"path": str(path)}
    if not persistent:
        result["path_note"] = "kept in the shot cache; pass path= to keep it somewhere"
    origin = (region[0], region[1]) if region else (0, 0)
    if region and not cropped_in_shell:
        _crop(path, region)
        result["cropped_by"] = "this server, after a full capture"
    if region:
        result["region"] = {"x": region[0], "y": region[1],
                            "width": region[2], "height": region[3]}
    if window is not None:
        occlusion = _occlusion(window)
        if occlusion:
            result["occluded"] = occlusion

    scale = float(a.get("scale") or 1.0)
    if not 0.05 <= scale <= 4.0:
        raise ToolError("scale must be between 0.05 and 4.0")

    annotate = a.get("annotate")
    inline = a.get("inline", True)

    # When the image is being handed over inline, resizing the PNG first is
    # wasted work -- measured 0.51s for scale:0.5 versus 0.32s for the full
    # capture, because it costs a decode and a PNG re-encode. The inline encoder
    # has to resize anyway, so scale is folded into its target size instead and
    # the file on disk stays at native resolution.
    if scale != 1.0 and not inline:
        _rescale(path, min(scale, 1.0))
        result["scale"] = scale

    # Annotation goes on at native resolution and is labelled in SCREEN
    # coordinates, so it survives whatever the image is scaled to afterwards.
    if annotate:
        result["annotated"] = _annotate(path, origin, annotate, a,
                                        1.0 if inline else min(scale, 1.0))

    native = _png_dimensions(path)
    result["bytes"] = path.stat().st_size
    result["dimensions"] = native

    if inline:
        native_long = max(_dimension_pair(native) or (MODEL_MAX_EDGE, 0))
        target_edge = max(16, min(MODEL_MAX_EDGE, int(native_long * scale)))
        _attach_inline(result, path, {**a, "max_edge": target_edge,
                                      "upscale": scale > 1.0})
        shown = result.get("shown")
        if isinstance(shown, dict):
            result["coordinate_note"] = _coordinate_note(shown["dimensions"],
                                                         native, origin)
    elif region:
        result["coordinate_note"] = (
            f"pixel (px, py) in this image is screen "
            f"({origin[0]} + px, {origin[1]} + py)."
        )
    return result


def _dimension_pair(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d+)x(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _coordinate_note(shown: str, native: str, origin: tuple[int, int]) -> str:
    """How to turn a pixel in the image the model is looking at into a click.

    This has to describe the IMAGE THAT WAS SENT, not the file on disk. They are
    different sizes now, and a note about the wrong one is worse than no note:
    it produces confident clicks in the wrong place.
    """
    shown_wh, native_wh = _dimension_pair(shown), _dimension_pair(native)
    ox, oy = origin
    if not shown_wh or not native_wh:                       # pragma: no cover
        return f"this image starts at screen ({ox}, {oy})."
    factor = native_wh[0] / shown_wh[0] if shown_wh[0] else 1.0
    if abs(factor - 1.0) < 0.005:
        if not ox and not oy:
            return "this image is 1:1 with the screen; pixel (px, py) is screen (px, py)."
        return f"pixel (px, py) in this image is screen ({ox} + px, {oy} + py)."
    return (f"this image is {shown} for a {native} area, so pixel (px, py) is screen "
            f"({ox} + px*{factor:.3f}, {oy} + py*{factor:.3f}). "
            "Any drawn labels are already in screen coordinates.")


def _png_dimensions(path: Path) -> str:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return f"{img.width}x{img.height}"
    except Exception:
        pass
    if shutil.which("file"):
        out = subprocess.run(["file", "-b", str(path)], capture_output=True,
                             text=True, timeout=15).stdout
        m = re.search(r"(\d+) x (\d+)", out)
        if m:
            return f"{m.group(1)}x{m.group(2)}"
    return ""


def _pillow():
    try:
        from PIL import Image, ImageDraw
    except Exception:                                       # pragma: no cover
        raise ToolError(
            "this needs Pillow (python3-pil) for image work and it is not installed"
        ) from None
    return Image, ImageDraw


# =========================================================================
# handing an image to the model instead of a path to one
# =========================================================================
#
# Measured on this machine, 2026-08-22, from real session transcripts: a
# screenshot that returns a path is followed by a Read of that path 61 times out
# of 62, and screenshot->Read->next-action takes a median of 14.0s. The same
# session's browser tool, which returns the image inline, went screenshot->next
# action in 7.9s. The capture itself is 0.23s. So essentially all of the cost of
# "look at the screen" is the extra model round trip, and removing it is worth
# more than every other optimisation in this file combined.
#
# The size rules are not arbitrary. Anthropic downscales any image whose long
# edge exceeds 1568px and bills roughly width*height/750 tokens, so sending a
# 1920x1080 PNG pays for a resize that happens anyway. Doing it here costs 40ms
# and turns 1843 tokens into what the caller actually needs.

MODEL_MAX_EDGE = 1568      # above this the API downscales anyway
INLINE_QUALITY = 75        # JPEG; 60 starts to smear small UI text
INLINE_MAX_BYTES = 3_500_000
_INLINE_KEY = "__inline_image__"


def _estimate_tokens(width: int, height: int) -> int:
    return int(width * height / 750)


def _encode_inline(path: Path, max_edge: int = MODEL_MAX_EDGE,
                   quality: int = INLINE_QUALITY, upscale: bool = False) -> dict:
    """A PNG on disk, as a JPEG the model can be handed directly.

    RGBA -> RGB is required, not cosmetic: the extension writes RGBA and JPEG
    has no alpha channel, so this raises OSError without it.

    LANCZOS rather than BILINEAR: this is a picture of TEXT, and the whole point
    is to still be able to read a menu item after the resize. Measured at 40ms
    for a full screen, which is not worth trading legibility for.
    """
    import base64
    Image, _ = _pillow()
    with Image.open(path) as img:
        img = img.convert("RGB")
        width, height = img.size
        longest = max(width, height)
        if longest > max_edge or (upscale and longest < max_edge):
            factor = max_edge / longest
            width, height = max(1, int(width * factor)), max(1, int(height * factor))
            img = img.resize((width, height), Image.LANCZOS)

        import io
        for attempt_quality in (quality, 55, 40):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=attempt_quality)
            raw = buf.getvalue()
            if len(raw) <= INLINE_MAX_BYTES:
                break
        else:                                               # pragma: no cover
            raise ToolError("this image will not compress to a sane size")

    return {
        "data": base64.b64encode(raw).decode("ascii"),
        "media_type": "image/jpeg",
        "dimensions": f"{width}x{height}",
        "bytes": len(raw),
        "tokens_estimate": _estimate_tokens(width, height),
        "quality": attempt_quality,
    }


# =========================================================================
# watching the screen settle, and telling a hit from a miss
# =========================================================================
#
# This exists because `pointer_click` reports focus:"unchanged" for both a
# successful press of a button that never takes focus (every Telegram inline
# button) and a click that hit dead space. They were indistinguishable without a
# second capture and a human look; now they differ by a number.
#
# Which number matters. Measured on a calculator window, 240x135 cells,
# 2026-08-22 -- the first two columns are what a "percent of cells that moved"
# metric sees, the last two are what this uses instead:
#
#                       cells>12   percent   cells>60   max delta
#   nothing happening          0     0.00%          0           0
#   click into dead space      0     0.00%          0          10
#   button press: clear       24     0.07%         16         180
#   button press: digit 7     16     0.05%         10         196
#   button press: digit 5     24     0.07%         15         173
#
# A percentage cannot separate those: a real button press moves 0.05% of a
# window and the first threshold tried (0.5%) called every one of them "nothing
# happened" -- confidently, in the result the model reads. The strong-cell count
# separates them by an order of magnitude, because UI changes are small in area
# and enormous in contrast: text appears, a button lights up, a menu opens.

FINGERPRINT = (240, 135)     # 32400 cells; ~8ms to compare in pure Python
CELL_NOISE = 12              # 0-255; below this is encoder and antialiasing jitter
STRONG_DELTA = 60            # a cell this different is new content, not jitter
STRONG_CELLS_STABLE = 2      # a blinking caret is one or two cells; ignore it
# Measured margin, gnome-calculator, 2026-08-22: a blinking text caret peaks at
# 11 strong cells on a completely idle window (it cycles 0 -> 11 -> 0 about once
# a second), while the smallest real button press moves 22. 12 sits between them.
# Blinkers that toggle during the settle window are masked out below, which is
# what makes this margin comfortable rather than lucky.
STRONG_CELLS_CHANGED = 12
STABLE_CHANGED_PCT = 0.1     # two frames this close are the same frame
CHANGE_FLOOR_PCT = 0.06      # backstop for a large, low-contrast change
SETTLE_MAX_S = 1.5
SETTLE_POLL_S = 0.12
SETTLE_MIN_FRAMES = 4       # enough of a window to see what blinks on its own


def _fingerprint(path: Path) -> bytes:
    """A greyscale thumbnail, as bytes. Cheap enough to take every frame."""
    Image, _ = _pillow()
    with Image.open(path) as img:
        return img.convert("L").resize(FINGERPRINT, Image.BOX).tobytes()


def _frame_delta(before: bytes, after: bytes, ignore: set[int] | None = None) -> dict:
    """How different two frames are, in the three ways that turn out to matter.

    `ignore` is the set of cells known to be moving on their own -- a caret, a
    spinner, a clock. They are excluded from the counts rather than from the
    picture.
    """
    if len(before) != len(after) or not before:
        return {"percent": 100.0, "strong_cells": len(after or b""), "max_delta": 255}
    moved = strong = peak = 0
    for index, (x, y) in enumerate(zip(before, after)):
        if ignore and index in ignore:
            continue
        delta = x - y if x > y else y - x
        if delta > CELL_NOISE:
            moved += 1
            if delta > STRONG_DELTA:
                strong += 1
        if delta > peak:
            peak = delta
    return {"percent": round(100.0 * moved / len(before), 3),
            "strong_cells": strong, "max_delta": peak}


def _ambient_cells(frames: list[bytes]) -> set[int]:
    """Cells that were moving by themselves while nothing was happening.

    A caret blinks on a roughly one-second cycle, so a two-frame baseline taken
    120ms apart usually catches one phase and calls the other phase a change --
    measured: 11 strong cells on an idle calculator, six times out of six. The
    settle loop is already sampling frames after the action; anything that
    changes BETWEEN those samples is something that changes on its own, and is
    not evidence that the action did anything.
    """
    moving: set[int] = set()
    for earlier, later in zip(frames, frames[1:]):
        if len(earlier) != len(later):
            continue
        for index, (x, y) in enumerate(zip(earlier, later)):
            if abs(x - y) > CELL_NOISE:
                moving.add(index)
    return moving


def _frame_change(before: bytes, after: bytes) -> float:
    """Percent of cells that moved more than sensor noise. Kept for callers that
    only want the one number (the settle loop's noise figure)."""
    return _frame_delta(before, after)["percent"]


def _changed_since(baseline: list[bytes], current: bytes,
                   ambient: set[int] | None = None,
                   floor_percent: float = CHANGE_FLOOR_PCT) -> dict:
    """Whether the screen differs from `baseline` because something HAPPENED.

    Three rules, each of which exists because the two simpler ones were tried
    and measured wrong on this machine:

      * strong cells OUTSIDE anything that was already moving, threshold 4.
        The cheap, confident case: a menu opened, a dialog appeared.
      * strong cells including them, threshold 12. Needed because a caret and
        the text it sits next to occupy the SAME cells -- masking the caret out
        masked the typed digit out with it, and a real button press dropped from
        22 strong cells to 0. 12 sits above the caret's measured peak of 11.
      * a large, high-contrast area change, for a scroll or a page swap, which
        moves a lot of cells without any of them being new content.

    The area rule needs the contrast test beside it: a click that only moved
    focus repainted 2.6% of a window with a peak difference of 17/255, and an
    area-only rule called that a change.
    """
    outside = _least_change(baseline, current, ambient) if ambient else None
    overall = _least_change(baseline, current)
    delta = dict(overall)
    if outside is not None:
        delta["strong_cells_excluding_moving"] = outside["strong_cells"]

    landed = (
        (outside is not None and outside["strong_cells"] >= 4)
        or overall["strong_cells"] >= STRONG_CELLS_CHANGED
        or (overall["percent"] > max(floor_percent, 1.0) and overall["max_delta"] > 40)
    )
    delta["landed"] = landed
    return delta


def _settle(path: Path, region: tuple[int, int, int, int] | None,
            max_wait: float = SETTLE_MAX_S) -> dict:
    """Capture until two consecutive frames agree, then return the last one.

    A fixed sleep is either too short (and catches a half-drawn menu) or too long
    (and is pure latency). Foreground `sleep` is blocked on this machine anyway.
    """
    start = time.monotonic()
    seen: list[bytes] = []
    noise = 0.0
    settled_at: int | None = None
    while True:
        _capture(path, region)
        current = _fingerprint(path)
        seen.append(current)
        waited = time.monotonic() - start
        if len(seen) >= 2:
            delta = _frame_delta(seen[-2], current)
            noise = delta["percent"]
            if (delta["strong_cells"] <= STRONG_CELLS_STABLE
                    and noise <= STABLE_CHANGED_PCT):
                settled_at = len(seen)
        # Keep sampling past the first agreement, up to SETTLE_MIN_FRAMES, so
        # there is enough of a window to see what moves on its own. Two frames
        # 120ms apart cannot tell a caret from a keystroke.
        if settled_at is not None and len(seen) >= SETTLE_MIN_FRAMES:
            # The delta between the frames that agreed IS the live noise floor
            # for this exact rectangle. Reporting it lets the verdict adapt
            # instead of trusting one constant for a dialog and a whole desktop.
            return {"settled": True, "frames": len(seen), "noise_percent": noise,
                    "waited_seconds": round(waited, 2), "fingerprint": current,
                    "ambient": _ambient_cells(seen)}
        if waited >= max_wait:
            return {"settled": settled_at is not None, "frames": len(seen),
                    "noise_percent": noise, "waited_seconds": round(waited, 2),
                    "fingerprint": current, "ambient": _ambient_cells(seen),
                    "detail": None if settled_at is not None else
                    "still changing when time ran out; the image is the last frame"}
        time.sleep(SETTLE_POLL_S)


def _look_region(a: dict, hint_window: dict | None,
                 point: tuple[float, float] | None) -> tuple[Any, tuple | None, dict | None]:
    """Resolve `look` into a rectangle. Returns (mode, region, window)."""
    look = a.get("look", "auto")
    if look is False or look == "none":
        return False, None, None
    if look is True:
        look = "auto"
    if look not in ("auto", "window", "screen", "region"):
        raise ToolError("look must be auto, window, screen, region, or false")

    if look == "screen":
        return look, None, None
    if look == "region":
        region = a.get("look_at") or a.get("region")
        if region is None:
            raise ToolError('look:"region" needs look_at: {x, y, width, height}')
        return look, _parse_region(region), None

    window = hint_window
    if window is None and a.get("expect_window") is not None:
        try:
            window = _resolve_target(a["expect_window"])
        except ToolError:
            window = None
    if window is None and point is not None:
        try:
            hit = window_at(point[0], point[1])
            # window_at answers {"window": {...}, "covering": [...]}. The
            # compositor's pick is the authority on WHICH window, but it carries
            # no geometry -- only id, class, title, focused. Taking it at face
            # value left every click looking at the whole screen instead of the
            # window it hit: 6x the tokens, 7x the wall clock, and a change
            # figure diluted until a real hit read as a miss.
            covering = hit.get("covering") or []
            picked = hit.get("window") or {}
            window = next((w for w in covering if w["id"] == picked.get("id")), None)
            if window is None:
                window = covering[0] if covering else None
        except ToolError:
            window = None
    if not window or window.get("width") is None:
        return look, None, None                             # fall back to the screen
    return look, (window["x"], window["y"], window["width"], window["height"]), window


class _Look:
    """The before-half of a look, carried across the action to the after-half.

    Resolving the rectangle costs two D-Bus round trips, so it is done once here
    rather than again on the way out.
    """

    __slots__ = ("mode", "region", "window", "before")

    def __init__(self, mode: Any, region: tuple | None, window: dict | None,
                 before: list[bytes] | None):
        self.mode, self.region, self.window, self.before = mode, region, window, before


def _baseline(path: Path, region: tuple[int, int, int, int] | None,
              gap: float = 0.12, frames: int = 2) -> list[bytes]:
    """Two frames of "before", a moment apart.

    One frame is not a baseline on a screen with anything blinking in it.
    gnome-calculator's entry has a caret, and a single-frame baseline reported a
    change 6 times out of 6 on a completely idle window: 5-11 strong cells,
    peaking at 207/255, purely from the caret being on in one frame and off in
    the other. Comparing against both frames and taking the smaller difference
    costs one extra capture -- 50ms for a window -- and makes a blink invisible
    while leaving real changes untouched.
    """
    out = []
    for index in range(max(2, frames)):
        if index:
            time.sleep(gap)
        _capture(path, region)
        out.append(_fingerprint(path))
    return out


def _least_change(baseline: list[bytes], current: bytes,
                  ignore: set[int] | None = None) -> dict:
    """How different `current` is from the baseline frame it resembles most."""
    deltas = [_frame_delta(b, current, ignore) for b in baseline if b]
    if not deltas:
        return {"percent": 0.0, "strong_cells": 0, "max_delta": 0}
    return min(deltas, key=lambda d: (d["strong_cells"], d["percent"]))


def _look_before(a: dict, hint_window: dict | None = None,
                 point: tuple[float, float] | None = None) -> _Look:
    """The screen as it was, so the action's effect can be measured."""
    try:
        mode, region, window = _look_region(a, hint_window, point)
    except ToolError:
        raise
    except Exception:                                       # pragma: no cover
        return _Look(False, None, None, None)
    if mode is False:
        return _Look(False, None, None, None)
    try:
        path, _ = _shot_path({})
        return _Look(mode, region, window, _baseline(path, region))
    except Exception:
        # Never let the measurement break the action it is measuring.
        return _Look(mode, region, window, None)


def _look(a: dict, result: dict, prepared: _Look) -> dict:
    """Attach what the screen looks like now, and how much the action changed it.

    `look:"auto"` (the default) always reports the change figure -- it costs one
    capture -- but only spends the tokens on an image when something actually
    moved. A click that changed nothing is the case you most need told about and
    the one you least need a picture of.
    """
    mode, region, window, before = (prepared.mode, prepared.region,
                                    prepared.window, prepared.before)
    if mode is False:
        return result

    path, _ = _shot_path({})
    settle = _settle(path, region, float(a.get("settle_max_s") or SETTLE_MAX_S))
    after = settle.pop("fingerprint")
    ambient = settle.pop("ambient", None) or set()

    view: dict[str, Any] = {"of": (window["wm_class"] if window else "whole screen"),
                            "settled": settle["settled"],
                            "frames": settle["frames"],
                            "waited_seconds": settle["waited_seconds"]}
    if settle.get("detail"):
        view["detail"] = settle["detail"]
    if ambient:
        view["moving_on_its_own"] = len(ambient)

    landed = None
    if before:
        floor = max(CHANGE_FLOOR_PCT, 2 * settle.get("noise_percent", 0.0))
        delta = _changed_since(before, after, ambient, floor)
        landed = delta.pop("landed")
        view["changed"] = delta
        view["verdict"] = (
            "the screen changed, so this landed on something"
            if landed else
            "NOTHING on screen changed -- if this was meant to press something, it missed"
        )

    show = mode != "auto" or landed is None or landed
    if show:
        origin = (region[0], region[1]) if region else (0, 0)
        native = _png_dimensions(path)
        _attach_inline(result, path, a)
        shown = result.get("shown")
        if isinstance(shown, dict):
            view["coordinate_note"] = _coordinate_note(shown["dimensions"], native, origin)
    else:
        view["image"] = "not attached: nothing changed, so there is nothing new to see"
    view["path"] = str(path)
    result["look"] = view
    return result


def _look_typed(a: dict, result: dict, window: dict, prepared: _Look) -> dict:
    """Prove typing landed with a picture, when AT-SPI readback is not available.

    Telegram, Chrome and Electron expose no editable text widget, so `type_text`
    returned verified:false and the caller spent a screenshot plus an image Read
    proving 20 characters arrived -- three sessions running, 12 wasted calls in
    one of them. The picture is the proof, so take it here rather than making
    the caller ask for it.
    """
    if prepared.mode is False:
        return result
    forced = {**a, "look": "window" if a.get("look", "auto") == "auto" else a["look"]}
    result = _look(forced, result, prepared)
    view = result.get("look")
    if isinstance(view, dict):
        view["why"] = ("attached because AT-SPI could not read the text back; "
                       "this picture is the only proof the characters arrived")
    return result


def _attach_inline(result: dict, path: Path, a: dict) -> dict:
    """Put the picture in the result unless the caller said not to."""
    if not a.get("inline", True):
        result["inline"] = False
        return result
    try:
        image = _encode_inline(
            path,
            max_edge=int(a.get("max_edge") or MODEL_MAX_EDGE),
            quality=int(a.get("quality") or INLINE_QUALITY),
            upscale=bool(a.get("upscale")),
        )
    except ToolError:
        raise
    except Exception as e:
        # An image that cannot be encoded must not lose the caller the capture:
        # the path is still on disk and still readable.
        result["inline"] = f"could not encode for inline viewing ({e}); Read {path}"
        return result
    result[_INLINE_KEY] = {"data": image.pop("data"),
                           "media_type": image["media_type"]}
    result["shown"] = image
    return result


def _parse_region(region: Any) -> tuple[int, int, int, int]:
    if isinstance(region, dict):
        try:
            values = (region["x"], region["y"], region["width"], region["height"])
        except KeyError:
            raise ToolError("region needs x, y, width, height") from None
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        values = tuple(region)
    else:
        raise ToolError("region must be [x, y, width, height] or an object with those keys")
    x, y, width, height = (int(v) for v in values)
    if width <= 0 or height <= 0:
        raise ToolError(f"refusing to capture a {width}x{height} region")
    return x, y, width, height


def _screenshot_region(a: dict) -> tuple[tuple[int, int, int, int] | None, dict | None]:
    """The rectangle to capture, and the window it came from if there was one.

    `window` and `region` together used to be refused. They now mean "this
    rectangle, measured inside that window", which is the shape the caller
    actually wanted every time it was refused: a 1200x100 chat composer strip is
    160 tokens against the 1843 of a full screen, and hand-converting it to
    absolute coordinates on every call is how clicks end up 40px off.
    """
    win = None
    if a.get("window") is not None:
        win = _resolve_target(a["window"])
        if win.get("minimized"):
            raise ToolError(
                f'{win["wm_class"]} is minimized, so there is nothing on screen to '
                "capture. Activate it first."
            )

    region = a.get("region")
    if region is None:
        if win is None:
            return None, None
        return (win["x"], win["y"], win["width"], win["height"]), win

    x, y, width, height = _parse_region(region)
    if win is None:
        return (x, y, width, height), None

    # window-relative, clamped to the window so a sloppy strip cannot wander
    # onto whatever is next to it.
    x, y = win["x"] + x, win["y"] + y
    width = min(width, win["x"] + win["width"] - x)
    height = min(height, win["y"] + win["height"] - y)
    if width <= 0 or height <= 0:
        raise ToolError(
            f'region does not fall inside {win["wm_class"]} '
            f'({win["width"]}x{win["height"]}); it is measured from the window\'s '
            "top-left corner when window= is given"
        )
    return (x, y, width, height), win


def _crop(path: Path, region: tuple[int, int, int, int]) -> None:
    Image, _ = _pillow()
    x, y, width, height = region
    with Image.open(path) as img:
        box = (max(0, x), max(0, y), min(img.width, x + width), min(img.height, y + height))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ToolError(f"region {region} does not overlap the screen")
        img.crop(box).save(path)


def _rescale(path: Path, scale: float) -> None:
    Image, _ = _pillow()
    with Image.open(path) as img:
        img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))
                   ).save(path)


def _label_font(size: int = 13):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        try:
            from PIL import ImageFont
            return ImageFont.load_default()
        except Exception:                                   # pragma: no cover
            return None


def _annotate(path: Path, origin: tuple[int, int], annotate: Any, a: dict,
              scale: float = 1.0) -> dict:
    """Draw the coordinate system onto the picture.

    A model looking at a screenshot has no way to turn "that button" into a
    number, and guessing from proportions is how a click ends up in the wrong
    window. Drawing the grid, the window boxes and the widget boxes -- all
    labelled in SCREEN coordinates, not image ones -- means the number to click
    can be read straight off the image.
    """
    Image, ImageDraw = _pillow()
    options = annotate if isinstance(annotate, dict) else {}
    if annotate is True:
        options = {"grid": True, "windows": True}
    grid = options.get("grid", True)
    spacing = int(grid) if isinstance(grid, int) and not isinstance(grid, bool) else 100
    drawn = {"grid_spacing": spacing if grid else None, "windows": 0, "widgets": 0}

    font = _label_font()

    with Image.open(path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        ox, oy = origin

        def px(sx: float, sy: float) -> tuple[float, float]:
            """Screen coordinates to pixels in this (cropped, scaled) image."""
            return (sx - ox) * scale, (sy - oy) * scale

        def text(sx: float, sy: float, label: str, colour) -> None:
            x, y = px(sx, sy)
            # A dark plate under the label, because pink on a white window and
            # pink on a dark one cannot both be read.
            box = draw.textbbox((x + 2, y + 2), label, font=font)
            draw.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1],
                           fill=(0, 0, 0))
            draw.text((x + 2, y + 2), label, fill=colour, font=font)

        if grid:
            width_s, height_s = img.width / scale, img.height / scale
            first_x = ((ox + spacing - 1) // spacing) * spacing
            for sx in range(int(first_x), int(ox + width_s), spacing):
                x, _ = px(sx, 0)
                draw.line([(x, 0), (x, img.height)], fill=(255, 0, 128), width=1)
                text(sx, oy, str(sx), (255, 120, 190))
            first_y = ((oy + spacing - 1) // spacing) * spacing
            for sy in range(int(first_y), int(oy + height_s), spacing):
                _, y = px(0, sy)
                draw.line([(0, y), (img.width, y)], fill=(255, 0, 128), width=1)
                text(ox, sy, str(sy), (255, 120, 190))

        if options.get("windows", True):
            for win in reversed(list_windows()):
                if win.get("minimized"):
                    continue
                x0, y0 = px(win["x"], win["y"])
                x1, y1 = px(win["x"] + win["width"], win["y"] + win["height"])
                draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 200, 255), width=2)
                text(win["x"], win["y"],
                     f'{win["id"]} {win["wm_class"]} @{win["x"]},{win["y"]}',
                     (0, 200, 255))
                drawn["windows"] += 1

        if options.get("widgets"):
            app = a.get("app")
            if not app:
                focused = [w for w in list_windows() if w.get("focused")]
                app = _atspi_app_for_window(focused[0]) if focused else None
            if app:
                for widget in _clickable_widgets(app, int(options.get("limit") or 40)):
                    b = widget["bounds"]
                    x0, y0 = px(b["x"], b["y"])
                    x1, y1 = px(b["x"] + b["w"], b["y"] + b["h"])
                    draw.rectangle([x0, y0, x1, y1], outline=(0, 255, 120), width=1)
                    text(b["x"], b["y"],
                         f'{widget["click_at"][0]},{widget["click_at"][1]} '
                         f'{widget["name"][:24]}', (0, 255, 120))
                    drawn["widgets"] += 1
                drawn["widgets_app"] = app
        img.save(path)
    return drawn


def tool_screencast(a: dict) -> dict:
    # Deliberately a subprocess rather than an import: screencast.py has to hold
    # the D-Bus connection open for the whole recording, because Mutter destroys
    # the session the instant that connection drops. Running it in-process would
    # block this server's stdio loop for the entire duration.
    path = Path(os.path.expanduser(str(a.get("path") or ""))).absolute()
    if path.is_dir():
        raise ToolError(f"{path} is a directory; give a file path ending in .mp4")
    if path.suffix.lower() != ".mp4":
        raise ToolError(f"path must end in .mp4, got {path.name!r}")
    path.parent.mkdir(parents=True, exist_ok=True)

    seconds = float(a.get("seconds", 10))
    if not 0.5 <= seconds <= 120:
        raise ToolError(f"seconds must be between 0.5 and 120, got {seconds}")

    cmd = [sys.executable, str(Path(__file__).parent / "screencast.py"),
           "--seconds", str(seconds), "--fps", str(int(a.get("fps", 30)))]
    if a.get("target") is not None:
        cmd += ["--window", str(_resolve_target(a["target"])["id"])]
    if a.get("include_cursor"):
        cmd.append("--cursor")
    cmd.append(str(path))

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=seconds + 60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise ToolError(f"recording failed: {detail[:400]}")
    result = json.loads(proc.stdout)
    # An mp4 is opaque to a model, so a bare path is a dead end -- the caller
    # would either try to Read the video or fall back to guessing. Name the
    # exact next call here, at the one moment it is certain to be read.
    result["next_step"] = (
        f'The mp4 cannot be read directly. Call frames{{"path": "{path}"}} to '
        "tile its frames into one PNG, then Read that PNG."
    )
    return result


def _frames_once(a: dict, raw: Any) -> dict:
    path = Path(os.path.expanduser(str(raw or ""))).absolute()
    if not path.exists():
        raise ToolError(f"{path} does not exist")
    if path.suffix.lower() not in (".mp4", ".mkv", ".webm", ".mov"):
        raise ToolError(f"expected a video file, got {path.name!r}")

    outdir = a.get("outdir")
    outdir = (Path(os.path.expanduser(str(outdir))).absolute() if outdir
              else path.parent / f"{path.stem}-frames")

    cols, rows = int(a.get("cols", 4)), int(a.get("rows", 3))
    if not (1 <= cols <= 8 and 1 <= rows <= 8):
        raise ToolError(f"cols and rows must each be 1-8, got {cols}x{rows}")

    cmd = [sys.executable, str(Path(__file__).parent / "frames.py"),
           str(path), str(outdir), "--cols", str(cols), "--rows", str(rows),
           "--json"]
    for flag, key in (("--from-frame", "from_frame"), ("--to-frame", "to_frame")):
        if a.get(key) is not None:
            cmd += [flag, str(int(a[key]))]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise ToolError("frame extraction failed: "
                        f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
    return json.loads(proc.stdout)


def tool_frames(a: dict) -> dict:
    """Contact sheet + motion measurement for a recording. See frames.py.

    The sheet comes back inline for the same reason screenshots do: it existed
    to be looked at, and returning a path meant a second round trip to Read it.
    `compare` stacks a second recording underneath the first, which is what a
    before/after actually needs -- two calls and two Reads used to mean judging
    two images that were never on screen together.
    """
    result = _frames_once(a, a.get("path"))
    sheet = result.get("contact_sheet") or result.get("sheet")

    other = a.get("compare")
    if other:
        second = _frames_once(a, other)
        result = {"first": result, "second": second}
        sheet2 = second.get("contact_sheet") or second.get("sheet")
        if sheet and sheet2:
            try:
                sheet = _stack_sheets(Path(sheet), Path(sheet2))
                result["compared_sheet"] = str(sheet)
            except Exception as e:                          # pragma: no cover
                result["compare_note"] = f"could not stack the two sheets: {e}"

    if sheet and a.get("inline", True):
        _attach_inline(result, Path(sheet), a)
    return result


def _stack_sheets(top: Path, bottom: Path) -> Path:
    """Two contact sheets in one image, first above second, same width."""
    Image, ImageDraw = _pillow()
    with Image.open(top) as a_img, Image.open(bottom) as b_img:
        a_rgb, b_rgb = a_img.convert("RGB"), b_img.convert("RGB")
        width = max(a_rgb.width, b_rgb.width)
        if b_rgb.width != width:
            b_rgb = b_rgb.resize((width, int(b_rgb.height * width / b_rgb.width)),
                                 Image.LANCZOS)
        if a_rgb.width != width:
            a_rgb = a_rgb.resize((width, int(a_rgb.height * width / a_rgb.width)),
                                 Image.LANCZOS)
        gap = 28
        out = Image.new("RGB", (width, a_rgb.height + b_rgb.height + gap * 2), "black")
        out.paste(a_rgb, (0, gap))
        out.paste(b_rgb, (0, a_rgb.height + gap * 2))
        draw = ImageDraw.Draw(out)
        font = _label_font(18)
        draw.text((8, 4), "FIRST", fill="white", font=font)
        draw.text((8, a_rgb.height + gap + 4), "SECOND", fill="white", font=font)
        target = top.with_name(f"{top.parent.name}-vs-{bottom.parent.name}.png")
        out.save(target)
    return target


def tool_activate_window(a: dict) -> dict:
    watching = _look_before(a)
    result = focus_window(a.get("target"))
    if watching.mode is not False:
        # Look at the window that was just raised, not whatever was in front
        # when the call started.
        watching.window = result["window"]
        watching.region = (result["window"]["x"], result["window"]["y"],
                           result["window"]["width"], result["window"]["height"])
    return _look(a, result, watching)


def tool_ui_apps(_: dict) -> dict:
    apps = list_atspi_apps()
    return {"count": len(apps), "apps": apps}


def tool_ui_tree(a: dict) -> dict:
    app = str(a.get("app") or "")
    if not app:
        raise ToolError("app is required (see ui_apps for the names on the bus)")
    depth = int(a.get("depth") or DEFAULT_TREE_DEPTH)
    node = _find_app(app)
    out: list[dict] = []
    _walk(node, node.get_name(), 0, depth, out)
    truncated = len(out) >= MAX_TREE_NODES
    return {"app": node.get_name(), "depth": depth, "nodes": len(out),
            "truncated": truncated,
            "note": (f"stopped at {MAX_TREE_NODES} nodes; narrow with ui_find "
                     "instead of raising depth") if truncated else "",
            "tree": out}


def tool_ui_find(a: dict) -> dict:
    text = str(a.get("text") or "")
    role = a.get("role")
    depth = int(a.get("depth") or DEFAULT_FIND_DEPTH)
    actionable_only = bool(a.get("actionable_only", False))
    if not text and not role and not actionable_only:
        raise ToolError(
            "give text, or role, or actionable_only:true. Without any of them this "
            "would dump the whole tree -- use ui_tree for that."
        )

    roots = [_find_app(str(a["app"]))] if a.get("app") else None
    if roots is None:
        Atspi = _atspi()
        desk = Atspi.get_desktop(0)
        roots = [desk.get_child_at_index(i) for i in range(desk.get_child_count())]
        roots = [r for r in roots if r is not None]

    needle = text.lower()
    hits: list[dict] = []
    unreachable: list[str] = []
    for root in roots:
        # One AppArmor-confined snap used to end the whole scan: hitting
        # snap.telegram-desktop raised out of the loop and ui_find returned
        # nothing at all, from every other application as well. An app that
        # cannot be read is a gap in the answer, not the end of it.
        try:
            name = root.get_name() or "<unnamed application>"
        except Exception:
            name = "<application that would not identify itself>"
        collected: list[dict] = []
        try:
            _walk(root, name, 0, depth, collected, cap=MAX_FIND_NODES)
        except Exception as e:
            unreachable.append(f"{name}: {str(e).strip()[:120] or type(e).__name__}")
            continue
        for node in collected:
            if needle and needle not in node["name"].lower() \
                    and needle not in node["path"].lower():
                continue
            if role and node["role"] != role:
                continue
            if actionable_only and not node.get("actions"):
                continue
            hits.append(node)
    out = {
        "query": text or (role or "actionable widgets"),
        "matches": len(hits), "results": hits[:40],
        "hint": ("pass the path plus expect_name or expect_role to ui_press; "
                 "pressing the real widget cannot miss, and paths go stale as soon "
                 "as the tree changes"),
    }
    if unreachable:
        out["unreachable_apps"] = unreachable
        out["unreachable_note"] = (
            "these applications refused to be read (AppArmor-confined snaps do "
            "this) and were skipped; anything inside them is not in these results. "
            "find_text reads them from the pixels instead."
        )
    return out


def tool_ui_press(a: dict) -> dict:
    path = str(a.get("path") or "")
    if not path:
        raise ToolError("path is required (get one from ui_find)")
    expect_name = a.get("expect_name")
    expect_role = a.get("expect_role")
    # An empty expect_name defeats the whole check, because "" is a substring of
    # every string -- expect_name="" would have matched a widget called
    # "Delete Everything". Treat blank as absent.
    if isinstance(expect_name, str) and not expect_name.strip():
        expect_name = None
    if isinstance(expect_role, str) and not expect_role.strip():
        expect_role = None
    if expect_name is None and expect_role is None:
        raise ToolError(
            "expect_name or expect_role is required, and neither may be blank. An "
            "AT-SPI index path is only valid while the tree is unchanged, so acting "
            "on one without checking what it now points at is how you press the "
            "wrong widget."
        )
    index = int(a.get("action_index") or 0)

    node = _resolve_path(path)
    actual_name = node.get_name() or ""
    actual_role = node.get_role_name()
    if expect_name is not None and str(expect_name).lower() not in actual_name.lower():
        raise ToolError(
            f"refusing to act: {path} now points at {actual_name!r} [{actual_role}], "
            f"not {expect_name!r}. The tree moved -- call ui_find again."
        )
    if expect_role is not None and expect_role != actual_role:
        raise ToolError(
            f"refusing to act: {path} is a {actual_role}, not a {expect_role}."
        )

    n_actions = node.get_n_actions() if node.get_action_iface() else 0
    if n_actions <= index:
        raise ToolError(
            f"{path} ({actual_name!r} [{actual_role}]) exposes {n_actions} action(s), "
            f"so action_index {index} does not exist"
        )
    action_name = node.get_localized_name(index)
    watching = _look_before(a)
    ok = node.do_action(index)
    if not ok:
        raise ToolError(f"do_action({index}) on {path} returned false; nothing happened")
    result = {"path": path, "widget": actual_name, "role": actual_role,
              "action": action_name,
              "detail": f"pressed {action_name!r} on {actual_name!r} [{actual_role}]"}
    return _look(a, result, watching)


def tool_ui_read_text(a: dict) -> dict:
    app = str(a.get("app") or "")
    path = a.get("path")
    if not app and not path:
        raise ToolError("app or path is required")
    node = _find_text_widget(app, path)
    content = _read_text(node)
    return {"path": path or "auto-located", "role": node.get_role_name(),
            "characters": len(content), "text": content}


def tool_ui_set_text(a: dict) -> dict:
    """Write text through AT-SPI EditableText -- no focus, no ydotool.

    This is the best text-entry path on this machine and it is worth knowing why.
    ydotool injects below the compositor and is focus-blind, so it needs a window
    activated first and can still lose the race. AT-SPI hands the characters
    straight to the widget: it works on an unfocused window, works while the
    screen is locked, and can be verified by reading the widget back.
    """
    text = a.get("text")
    if not isinstance(text, str):
        raise ToolError("text is required")
    app = str(a.get("app") or "")
    path = a.get("path")
    if not app and not path:
        raise ToolError("app or path is required")
    replace = bool(a.get("replace", False))

    node = _find_text_widget(app, path)
    Atspi = _atspi()
    text_iface, editable = _text_ifaces(node)
    if editable is None:
        raise ToolError(
            f"{node.get_role_name()} {node.get_name()!r} is not editable through "
            "AT-SPI. Use type_text with an explicit target window instead, "
            "accepting that injection is focus-blind."
        )

    before = _read_text(node)
    if replace and before:
        Atspi.EditableText.delete_text(editable, 0, len(before))
    offset = 0 if replace else Atspi.Text.get_character_count(text_iface)
    if not node.insert_text(offset, text, len(text)):
        raise ToolError("insert_text returned false; nothing was written")

    time.sleep(0.2)
    after = _read_text(node)
    if text not in after:
        raise ToolError(
            "insert_text reported success but the text is not in the widget "
            f"(now {len(after)} chars). Treat this as a failure, not a success."
        )
    # With replace=True, `text in after` is too weak: a no-op delete_text leaves
    # the old content, the new text is found anyway, and the tool would report
    # verified:True on a widget that was never actually cleared.
    if replace and after.strip() != text.strip():
        raise ToolError(
            f"replace=True did not clear the widget: it holds {len(after)} chars "
            f"but {len(text)} were written. delete_text appears to be a no-op on "
            f"this widget ({node.get_role_name()}); content now starts "
            f"{after[:60]!r}."
        )
    return {"role": node.get_role_name(), "wrote": len(text),
            "characters_before": len(before), "characters_after": len(after),
            "verified": True,
            "detail": f"wrote {len(text)} chars and read them back out of the widget"}


def tool_type_text(a: dict) -> dict:
    text = a.get("text")
    if not isinstance(text, str) or text == "":
        raise ToolError("text is required")
    target = a.get("target")
    if target is None:
        raise ToolError(
            "target is required. ydotool injection is focus-blind, so typing "
            "without naming a window means typing into whatever is in front. Pass a "
            "window id or wm_class from list_windows."
        )
    focus = focus_window(target)
    delay = int(a.get("key_delay_ms") or 20)
    watching = _look_before(a, hint_window=focus["window"])

    # Read the widget BEFORE typing so we can tell what this call actually added.
    app_hint = a.get("verify_app") or _atspi_app_for_window(focus["window"])
    before = None
    if app_hint:
        try:
            before = _read_text(_find_text_widget(str(app_hint), None))
        except ToolError:
            before = None

    # Keysyms first: the compositor is handed the CHARACTER, so the active XKB
    # layout cannot transpose it. ydotool is the fallback, and the reason this
    # function still has a verification pass at all.
    via = str(a.get("via") or "auto").lower()
    if via not in ("auto", "keysym", "ydotool"):
        raise ToolError("via must be auto, keysym or ydotool")
    used = "ydotool"
    if via in ("auto", "keysym"):
        try:
            _pointer().type_text(text, delay=max(delay, 8) / 1000)
            used = "compositor keysyms"
        except Exception as e:
            if via == "keysym":
                raise ToolError(f"keysym typing failed: {e}") from None
            _ydotool("type", "--key-delay", str(delay), text,
                     timeout=max(30.0, len(text) * delay / 1000 + 15))
    else:
        _ydotool("type", "--key-delay", str(delay), text,
                 timeout=max(30.0, len(text) * delay / 1000 + 15))

    result = {"characters": len(text), "focus": focus["detail"], "via": used,
              "detail": f'sent {len(text)} characters to {focus["window"]["wm_class"]}'}
    hazard = layout_hazard() if used == "ydotool" else ""
    if hazard:
        result["layout_warning"] = hazard

    # Verify what LANDED, not what was sent. ydotool exits 0 whether or not the
    # right characters arrived, and on a non-US layout they demonstrably do not.
    if before is None:
        result["verified"] = False
        result["detail"] += (" -- no readable AT-SPI text widget (Qt, Electron and "
                             "Chrome expose none), so this is verified by picture "
                             "instead of by readback"
                             + (f". {hazard}" if hazard else ""))
        return _look_typed(a, result, focus["window"], watching)

    time.sleep(0.4)
    try:
        after = _read_text(_find_text_widget(str(app_hint), None))
    except ToolError:
        result["verified"] = False
        result["detail"] += " -- could not re-read the widget; verified by picture instead"
        return _look_typed(a, result, focus["window"], watching)

    added = after[len(before):] if after.startswith(before) else after
    if text in added or text in after[len(before):]:
        result["verified"] = True
        result["detail"] = (f'typed {len(text)} characters into '
                            f'{focus["window"]["wm_class"]} and read them back')
        # Readback already proved it; a picture would be pure token spend.
        return _look(a, result, watching) if a.get("look") not in (None, "auto") \
            else result

    raise ToolError(
        f"{used} reported success but the wrong characters arrived. Sent {text!r}, "
        f"the widget gained {added!r}. Nothing here is retryable -- with ydotool "
        f"this is the keycode/layout mismatch, not a race. {hazard or ''} "
        "Use ui_set_text instead: it hands characters to the widget and cannot be "
        "transposed."
    )


def _atspi_app_for_window(window: dict) -> str | None:
    """Best-effort map a window's wm_class to an AT-SPI application name.

    They are not the same string: gnome-text-editor's wm_class is
    'org.gnome.TextEditor' while its AT-SPI name is 'gnome-text-editor'. Compare
    with separators and case stripped.
    """
    def norm(s: str) -> str:
        return "".join(c for c in (s or "").lower() if c.isalnum())

    wanted = norm(window.get("wm_class"))
    if not wanted:
        return None
    try:
        names = [a["name"] for a in list_atspi_apps()]
    except ToolError:
        return None
    for name in names:
        n = norm(name)
        if n and (n in wanted or wanted in n):
            return name
    return None


def tool_press_keys(a: dict) -> dict:
    combo = a.get("combo")
    if not combo:
        raise ToolError("combo is required, e.g. 'ctrl+s'")
    target = a.get("target")
    if target is None:
        raise ToolError(
            "target is required. Key injection is focus-blind; a combo sent at the "
            "wrong window can do real damage. Pass a window id or wm_class."
        )
    codes = parse_combo(combo)          # validate BEFORE stealing focus
    focus = focus_window(target)
    watching = _look_before(a, hint_window=focus["window"])

    # Same reasoning as type_text: a keysym is the key the user means, a keycode
    # is a position on a US keyboard that may hold a different key here.
    via = str(a.get("via") or "auto").lower()
    used = "ydotool"
    if via in ("auto", "keysym"):
        try:
            syms = combo_keysyms(combo)
            _pointer().combo(syms)
            used = "compositor keysyms"
        except Exception as e:
            if via == "keysym":
                raise ToolError(f"keysym combo failed: {e}") from None
            used = "ydotool"
    if used == "ydotool":
        sequence = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        _ydotool("key", "--key-delay", "40", *sequence)
    result = {"combo": combo, "focus": focus["detail"], "via": used,
              "detail": f'sent {combo} to {focus["window"]["wm_class"]}'}
    return _look(a, result, watching)


def _pointer_result(x: float, y: float, action: str, guard: dict | None) -> dict:
    out = {"action": action, "x": x, "y": y}
    if guard:
        out["guard"] = guard
    return out


def tool_pointer_move(a: dict) -> dict:
    x, y = _point(a)
    guard = _guard_point(x, y, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x, y))
    _pointer().move_to(x, y)
    result = _pointer_result(x, y, "moved", guard)
    return _look(a, result, watching)


def tool_pointer_click(a: dict) -> dict:
    x, y = _point(a)
    button = str(a.get("button") or "left")
    count = int(a.get("count") or 1)
    if not 1 <= count <= 3:
        raise ToolError("count must be 1, 2 or 3")
    guard = _guard_point(x, y, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x, y))
    before = [w for w in list_windows() if w.get("focused")]
    _pointer().click(x, y, button=button, count=count)

    result = _pointer_result(x, y, f"{button} click x{count}", guard)
    # Settling before reading focus is both more accurate and cheaper than the
    # fixed 0.25s sleep this used to take: a window that is going to take focus
    # has finished drawing by the time two frames agree.
    if watching.mode is False:
        time.sleep(0.25)
    else:
        _look(a, result, watching)
        watching = _Look(False, None, None, None)
    after = [w for w in list_windows() if w.get("focused")]
    # Whether the keyboard moved is the single most useful consequence of a
    # click and the caller cannot see it any other way. It is not sufficient on
    # its own: a Telegram inline button never takes focus, so a successful press
    # and a click into dead space both read "unchanged" here. `look` settles that.
    was = before[0]["wm_class"] if before else None
    now = after[0]["wm_class"] if after else None
    result["focus"] = ("unchanged: " + str(now)) if was == now else f"moved {was} -> {now}"
    return _look(a, result, watching)


def tool_pointer_drag(a: dict) -> dict:
    x1, y1 = _point(a, "from_x", "from_y")
    x2, y2 = _point(a, "to_x", "to_y")
    guard = _guard_point(x1, y1, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x2, y2))
    _pointer().drag(x1, y1, x2, y2, button=str(a.get("button") or "left"),
                           steps=int(a.get("steps") or 24))
    result = {"action": "drag", "from": [x1, y1], "to": [x2, y2], "guard": guard}
    return _look(a, result, watching)


def tool_pointer_scroll(a: dict) -> dict:
    x, y = _point(a)
    dy, dx = int(a.get("dy") or 0), int(a.get("dx") or 0)
    if not dy and not dx:
        raise ToolError("give dy (down is positive) or dx (right is positive)")
    if max(abs(dy), abs(dx)) > 30:
        raise ToolError("more than 30 wheel clicks at once is almost always a typo")
    guard = _guard_point(x, y, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x, y))
    _pointer().scroll(x, y, dy=dy, dx=dx)
    result = {"action": "scroll", "x": x, "y": y, "dy": dy, "dx": dx, "guard": guard}
    return _look(a, result, watching)


def tool_pointer_position(_: dict) -> dict:
    return pointer_position()


def tool_window_at(a: dict) -> dict:
    x, y = _point(a)
    return window_at(x, y)


def tool_screen_map(a: dict) -> dict:
    """Everything on screen with the pixel coordinates to reach it."""
    windows = list_windows()
    stack = [
        {**w, "centre": [w["x"] + w["width"] // 2, w["y"] + w["height"] // 2]}
        for w in reversed(windows)                  # topmost first
    ]
    out: dict[str, Any] = {
        "desktop": None,
        "windows_top_first": stack,
        "pointer": None,
    }
    try:
        x, y, width, height = _pointer().desktop_bounds()
        out["desktop"] = {"x": x, "y": y, "width": width, "height": height}
    except Exception as e:
        out["desktop"] = f"unknown: {e}"
    try:
        out["pointer"] = pointer_position()
    except ToolError as e:
        out["pointer"] = f"unknown: {e}"

    if a.get("widgets", True):
        app = a.get("app")
        if not app:
            focused = [w for w in windows if w.get("focused")]
            app = _atspi_app_for_window(focused[0]) if focused else None
        if app:
            try:
                out["widgets"] = _clickable_widgets(app, int(a.get("limit") or 60))
                out["widgets_app"] = app
            except ToolError as e:
                out["widgets"] = f"unavailable: {e}"
        else:
            out["widgets"] = ("no AT-SPI application matched the focused window; "
                              "pass app= to map a different one")
    return out


def _clickable_widgets(app_name: str, limit: int) -> list[dict]:
    """Widgets that can be pressed, each with the screen box to press it at.

    The AT-SPI tree already carries screen extents, so this is the answer to
    "where do I click for X" without measuring anything off a screenshot. Press
    them with ui_press where possible -- it cannot miss -- and use these
    coordinates when the widget only responds to a real pointer.
    """
    app = _find_app(app_name)
    collected: list[dict] = []
    _walk(app, app.get_name(), 0, DEFAULT_FIND_DEPTH, collected, cap=MAX_FIND_NODES)
    out = []
    for node in collected:
        bounds = node.get("bounds")
        if not bounds or not node.get("actions"):
            continue
        if bounds["w"] < 4 or bounds["h"] < 4:
            continue
        out.append({
            "name": node["name"], "role": node["role"], "path": node["path"],
            "bounds": bounds,
            "click_at": [bounds["x"] + bounds["w"] // 2, bounds["y"] + bounds["h"] // 2],
        })
        if len(out) >= limit:
            break
    return out


def tool_wait_for(a: dict) -> dict:
    """Poll a desktop condition instead of sleeping and hoping."""
    timeout = float(a.get("timeout") or 10)
    if not 0.2 <= timeout <= 120:
        raise ToolError("timeout must be between 0.2 and 120 seconds")
    condition = str(a.get("condition") or "").strip()
    target = a.get("target")
    known = {"window_focused", "window_exists", "window_gone", "focus_changes"}
    if condition not in known:
        raise ToolError(f"condition must be one of: {', '.join(sorted(known))}")
    if condition != "focus_changes" and target is None:
        raise ToolError(f"{condition} needs a target window")

    def matches(w: dict) -> bool:
        if isinstance(target, int) or (isinstance(target, str) and str(target).isdigit()):
            return w["id"] == int(target)
        needle = str(target).lower()
        return (needle in (w["wm_class"] or "").lower()
                or needle in (w["title"] or "").lower())

    start = time.monotonic()
    first = [w for w in list_windows() if w.get("focused")]
    was = first[0]["id"] if first else None
    while True:
        windows = list_windows()
        hits = [w for w in windows if matches(w)] if target is not None else []
        focused = [w for w in windows if w.get("focused")]
        now = focused[0]["id"] if focused else None
        met = (
            (condition == "window_exists" and hits)
            or (condition == "window_gone" and not hits)
            or (condition == "window_focused" and any(w.get("focused") for w in hits))
            or (condition == "focus_changes" and now != was)
        )
        waited = round(time.monotonic() - start, 2)
        if met:
            return {"condition": condition, "met": True, "waited_seconds": waited,
                    "focused": (focused[0]["wm_class"] if focused else None),
                    "matched": [{"id": w["id"], "wm_class": w["wm_class"],
                                 "title": w["title"]} for w in hits[:5]]}
        if waited >= timeout:
            return {"condition": condition, "met": False, "waited_seconds": waited,
                    "focused": (focused[0]["wm_class"] if focused else None),
                    "detail": "timed out; nothing was changed by waiting"}
        time.sleep(0.15)


# =========================================================================
# reading the screen without spending a picture on it
# =========================================================================

OCR_MIN_CONFIDENCE = 55
OCR_PSM_REGION = 6          # "one uniform block" -- a window or a toolbar
OCR_PSM_SCREEN = 11         # "sparse text" -- a whole desktop of separate widgets
OCR_UPSCALE_UNDER = 700     # px on the long edge; below this, OCR needs help


def tool_find_text(a: dict) -> dict:
    """Where a piece of visible text is, in screen coordinates.

    AT-SPI is the right answer and is tried first everywhere else in this file,
    but Chrome exposes three actionable nodes for an entire browser, Electron
    two, and Telegram's Qt tree is unreachable behind AppArmor. For those, the
    only thing that knows where the "Send" button is, is the picture -- and
    finding it currently means a screenshot, an image Read, and arithmetic on a
    scaled crop, which is 14 seconds and has put clicks 40px off.

    This is that loop, done here: about 1.5s and no image in the transcript.
    """
    needle = str(a.get("text") or "").strip()
    if not needle:
        raise ToolError("text is required: the visible string to look for")
    if not shutil.which("tesseract"):
        raise ToolError("this needs tesseract (tesseract-ocr) and it is not installed")

    region, window = _screenshot_region(a)
    path, _ = _shot_path({})
    _capture(path, region)
    origin = (region[0], region[1]) if region else (0, 0)

    # Measured 2026-08-22: on a 360x616 window psm 6 reads 35 words against psm
    # 11's 16; on the whole 1920x1080 desktop psm 6 reads 290 and MISSES the
    # calculator entirely while psm 11 reads 309 and finds it. One block of text
    # is what a window is and what a desktop is not.
    default_psm = OCR_PSM_REGION if region else OCR_PSM_SCREEN
    words, scale = _ocr_words(path, int(a.get("min_confidence") or OCR_MIN_CONFIDENCE),
                              int(a.get("psm") or default_psm))
    matches = _match_phrase(words, needle, bool(a.get("exact")))

    results = []
    for m in matches:
        x = origin[0] + m["x"] / scale
        y = origin[1] + m["y"] / scale
        w, h = m["width"] / scale, m["height"] / scale
        results.append({
            "text": m["text"],
            "click_at": [round(x + w / 2), round(y + h / 2)],
            "bounds": {"x": round(x), "y": round(y),
                       "width": round(w), "height": round(h)},
            "confidence": m["confidence"],
        })
    results.sort(key=lambda r: (-r["confidence"], r["bounds"]["y"]))
    limit = int(a.get("limit") or 10)

    out: dict[str, Any] = {
        "query": needle,
        "matches": len(results),
        "results": results[:limit],
        "searched": ({"window": window["wm_class"]} if window else
                     {"region": list(region)} if region else {"screen": True}),
        "words_read": len(words),
    }
    if not results:
        out["detail"] = (
            f"{len(words)} words were read and none matched {needle!r}. OCR misses "
            "icon-only buttons and low-contrast text entirely; try ui_find if the "
            "app has an accessibility tree, or take a picture and look."
        )
    return out


def _ocr_words(path: Path, min_confidence: int,
               psm: int = OCR_PSM_REGION) -> tuple[list[dict], float]:
    """Every legible word with its box, plus the factor its coordinates are in.

    Small captures are upscaled first: tesseract wants roughly 300dpi text and
    UI text at 1:1 is closer to 96, so a 360px-wide dialog reads as noise until
    it is enlarged. Measured on this machine: a full 1920x1080 screen is 3.7s,
    half of one is 1.6s.
    """
    Image, _ = _pillow()
    scale = 1.0
    source = path
    with Image.open(path) as img:
        if max(img.size) < OCR_UPSCALE_UNDER:
            scale = 2.0
            source = path.with_name(f".{path.name}.ocr.png")
            img.convert("RGB").resize((int(img.width * scale), int(img.height * scale)),
                                      Image.LANCZOS).save(source)
    try:
        # psm 6 -- "one uniform block of text" -- rather than the default 3.
        # Measured on a calculator window: psm 3 reads 9 words, psm 6 reads 35.
        # Page segmentation is looking for paragraphs and columns, and a toolbar
        # is neither, so it discards most of the interface as non-text.
        proc = subprocess.run(["tesseract", str(source), "stdout",
                               "--psm", str(psm), "tsv"],
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ToolError("tesseract did not finish within 60s") from None
    finally:
        if source is not path:
            source.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise ToolError(f"tesseract failed: {(proc.stderr or '').strip()[:300]}")

    words = []
    for line in proc.stdout.splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) < 12 or not cells[11].strip():
            continue
        try:
            confidence = float(cells[10])
        except ValueError:
            continue
        if confidence < min_confidence:
            continue
        words.append({"text": cells[11], "confidence": round(confidence),
                      "line": tuple(cells[1:5]),
                      "x": int(cells[6]), "y": int(cells[7]),
                      "width": int(cells[8]), "height": int(cells[9])})
    return words, scale


def _match_phrase(words: list[dict], needle: str, exact: bool) -> list[dict]:
    """Single words, and runs of consecutive words on one line for phrases."""
    wanted = needle if exact else needle.lower()

    def norm(s: str) -> str:
        return s if exact else s.lower()

    hits = []
    for word in words:
        text = norm(word["text"])
        if text == wanted or (not exact and wanted in text):
            hits.append(dict(word))

    if " " in needle:
        parts = wanted.split()
        for i in range(len(words) - len(parts) + 1):
            run = words[i:i + len(parts)]
            if any(w["line"] != run[0]["line"] for w in run):
                continue
            if [norm(w["text"]) for w in run] != parts:
                continue
            left = min(w["x"] for w in run)
            top = min(w["y"] for w in run)
            hits.append({
                "text": " ".join(w["text"] for w in run),
                "confidence": round(sum(w["confidence"] for w in run) / len(run)),
                "x": left, "y": top,
                "width": max(w["x"] + w["width"] for w in run) - left,
                "height": max(w["y"] + w["height"] for w in run) - top,
            })
    return hits


def tool_region_changed(a: dict) -> dict:
    """Wait until part of the screen changes, without spending a picture a poll.

    `wait_for` watches windows appearing and focus moving; it has nothing to say
    about a message arriving in a chat or a spinner finishing. That was polled
    with blind screenshots -- three full captures plus three image Reads for one
    15-second reply -- because there was no other way to ask.
    """
    timeout = float(a.get("timeout") or 10)
    if not 0.2 <= timeout <= 300:
        raise ToolError("timeout must be between 0.2 and 300 seconds")
    poll = max(0.1, float(a.get("poll_seconds") or 0.3))

    region, window = _screenshot_region(a)
    path, _ = _shot_path({})
    # A waiting tool can afford a proper look at what is already moving: four
    # frames over about half a second, and anything that toggles between them is
    # a caret or a spinner rather than the event being waited for.
    baseline = _baseline(path, region, gap=max(0.12, min(poll, 0.2)), frames=4)
    ambient = _ambient_cells(baseline)

    start = time.monotonic()
    polls, best = 1, {"percent": 0.0, "strong_cells": 0, "max_delta": 0}
    while True:
        waited = time.monotonic() - start
        if waited >= timeout:
            return {"changed": False, "waited_seconds": round(waited, 2),
                    "polls": polls, "largest_change_seen": best,
                    "watched": (window["wm_class"] if window else
                                list(region) if region else "whole screen"),
                    "moving_on_its_own": len(ambient),
                    "detail": "nothing changed before the timeout; nothing was altered "
                              "by waiting"}
        time.sleep(poll)
        _capture(path, region)
        polls += 1
        delta = _changed_since(baseline, _fingerprint(path), ambient)
        landed = delta.pop("landed")
        if delta["strong_cells"] > best["strong_cells"]:
            best = delta
        if landed:
            result = {"changed": True, "waited_seconds": round(time.monotonic() - start, 2),
                      "polls": polls, "change": delta,
                      "watched": (window["wm_class"] if window else
                                  list(region) if region else "whole screen")}
            if a.get("look", True) is not False:
                _attach_inline(result, path, a)
                shown = result.get("shown")
                if isinstance(shown, dict):
                    origin = (region[0], region[1]) if region else (0, 0)
                    result["coordinate_note"] = _coordinate_note(
                        shown["dimensions"], _png_dimensions(path), origin)
            return result


DO_STEPS_MAX = 24

# Verb -> (handler, required keys). Every one of these is an existing tool: a
# step is not a new capability, it is the same call without its own round trip.
_STEP_VERBS: dict[str, tuple[str, tuple[str, ...]]] = {
    "activate": ("activate_window", ("target",)),
    "click":    ("pointer_click", ()),
    "move":     ("pointer_move", ()),
    "drag":     ("pointer_drag", ()),
    "scroll":   ("pointer_scroll", ()),
    "type":     ("type_text", ("target", "text")),
    "key":      ("press_keys", ("target", "combo")),
    "press":    ("ui_press", ("path",)),
    "set_text": ("ui_set_text", ("path",)),
    "wait":     ("wait_for", ("condition",)),
}


def tool_do_steps(a: dict) -> dict:
    """Run several actions in one call, and look once at the end.

    Every action in a UI sequence used to cost its own model round trip, and the
    round trip is the expensive part -- median 7.9s against 0.23s of actual
    work. Typing into a dialog and confirming it was type_text, press_keys,
    screenshot, Read: four turns, about 35 seconds, for two keystrokes.

    Steps run with their own `look` forced off; the picture is taken once, after
    the last one, when it is the only picture anybody wanted.
    """
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError('steps must be a non-empty list, e.g. '
                        '[{"do":"click","x":100,"y":200},{"do":"key","target":"gedit",'
                        '"combo":"ctrl+s"}]')
    if len(steps) > DO_STEPS_MAX:
        raise ToolError(f"{len(steps)} steps is more than the {DO_STEPS_MAX} allowed "
                        "in one call; a sequence that long should be checked partway")

    stop_on_error = a.get("stop_on_error", True)
    done: list[dict] = []
    failed_at: int | None = None
    last_window: dict | None = None

    # The "before" has to be taken before the first step, not after the last
    # one, or the change figure measures nothing. Scope it to whatever the first
    # step names; if the sequence ends up somewhere else, the figure is dropped
    # rather than quietly reported against the wrong rectangle.
    first_hint = None
    for step in steps:
        if isinstance(step, dict) and step.get("target") is not None:
            try:
                first_hint = _resolve_target(step["target"])
            except ToolError:
                first_hint = None
            break
    first_point = None
    for step in steps:
        if isinstance(step, dict) and step.get("x") is not None:
            first_point = (float(step["x"]), float(step.get("y") or 0))
            break
    watching = _look_before(a, hint_window=first_hint, point=first_point)

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ToolError(f"step {index} is not an object")
        verb = str(step.get("do") or "").strip().lower()
        if verb == "wait_ms":
            verb = "sleep"
        if verb == "sleep":
            millis = int(step.get("ms") or step.get("wait_ms") or 200)
            if not 0 < millis <= 10_000:
                raise ToolError("sleep ms must be between 1 and 10000")
            time.sleep(millis / 1000)
            done.append({"step": index, "do": "sleep", "ok": True, "ms": millis})
            continue

        if verb not in _STEP_VERBS:
            raise ToolError(f"step {index}: unknown do {verb!r}. Known: sleep, "
                            + ", ".join(sorted(_STEP_VERBS)))
        tool_name, required = _STEP_VERBS[verb]
        missing = [k for k in required if step.get(k) is None]
        if missing:
            raise ToolError(f"step {index} ({verb}) needs {', '.join(missing)}")

        args = {k: v for k, v in step.items() if k != "do"}
        args["look"] = False            # one picture per call, not one per step
        try:
            out = HANDLERS[tool_name](args)
            done.append({"step": index, "do": verb, "ok": True,
                         "detail": _step_detail(out)})
            if isinstance(out, dict) and isinstance(out.get("window"), dict):
                last_window = out["window"]
            elif step.get("target") is not None:
                try:
                    last_window = _resolve_target(step["target"])
                except ToolError:
                    pass
            elif step.get("x") is not None:
                try:
                    hit = window_at(float(step["x"]), float(step.get("y") or 0))
                    covering = hit.get("covering") or []
                    picked = (hit.get("window") or {}).get("id")
                    last_window = next((w for w in covering if w["id"] == picked),
                                       covering[0] if covering else None)
                except ToolError:
                    pass
        except ToolError as e:
            done.append({"step": index, "do": verb, "ok": False, "error": str(e)})
            failed_at = index
            if stop_on_error:
                break

    result: dict[str, Any] = {
        "steps_run": len(done),
        "steps_given": len(steps),
        "all_ok": failed_at is None and len(done) == len(steps),
        "results": done,
    }
    if failed_at is not None:
        result["failed_at_step"] = failed_at
        result["detail"] = (f"step {failed_at} failed; "
                            + ("the rest were skipped" if stop_on_error
                               else "the rest still ran"))

    # A failure is exactly when the picture is worth having, so force one.
    look = dict(a)
    if failed_at is not None and look.get("look", "auto") == "auto":
        look["look"] = "window" if last_window else "screen"

    if last_window and (not watching.window
                        or watching.window.get("id") != last_window.get("id")):
        # The sequence moved to a different window than the one measured at the
        # start. Look at where it ended up, and say nothing about "changed".
        watching = _Look(watching.mode if watching.mode is not False else "window",
                         (last_window["x"], last_window["y"],
                          last_window["width"], last_window["height"]),
                         last_window, None)
    return _look(look, result, watching)


def _step_detail(out: Any) -> str:
    if not isinstance(out, dict):
        return str(out)[:160]
    for key in ("detail", "verdict", "action", "combo"):
        if out.get(key):
            return str(out[key])[:160]
    return json.dumps({k: v for k, v in out.items()
                       if not k.startswith("_") and k != "look"})[:160]


def tool_health(_: dict) -> dict:
    """Whether each mechanism is actually usable right now."""
    report: dict[str, Any] = {}

    report["extension"] = _extension_state()
    if report["extension"] != "ACTIVE":
        report["extension_diagnosis"] = _extension_diagnosis()

    try:
        report["windows"] = len(list_windows())
    except ToolError as e:
        report["windows"] = f"FAIL: {e}"

    try:
        report["atspi_apps"] = len(list_atspi_apps())
    except ToolError as e:
        report["atspi_apps"] = f"FAIL: {e}"

    # "ready" used to mean "the socket is there", which is not the question a
    # caller is asking. ydotool's absolute mode is dead on this machine and its
    # relative motion is put through pointer acceleration, and reporting it as
    # simply ready is what sent an agent down a two-hour detour on 2026-08-16.
    if not shutil.which("ydotool"):
        report["ydotool"] = "not installed"
    elif not os.path.exists(YDOTOOL_SOCKET):
        report["ydotool"] = "unavailable (ydotoold socket missing)"
    else:
        report["ydotool"] = ("present, but relative-only and acceleration-warped: "
                             "use pointer_move / pointer_click instead")

    try:
        ri = _input().shared()
        bounds = ri.desktop_bounds()
        report["pointer_control"] = (
            f"absolute, via org.gnome.Mutter.RemoteDesktop; desktop is "
            f"{bounds[2]}x{bounds[3]} at ({bounds[0]}, {bounds[1]})"
        )
    except Exception as e:
        report["pointer_control"] = f"FAIL: {e}"

    methods = extension_methods()
    missing = sorted({"Pointer", "WindowAt", "ScreenshotArea", "ScreenshotWindow"}
                     - methods)
    report["extension_methods"] = sorted(methods)
    if missing:
        report["extension_pending_relogin"] = (
            f"the running shell has no {', '.join(missing)}. "
            + _needs_relogin(missing[0])
        )
    report["xtest"] = (
        "avoid: XTEST through XWayland (xdotool, wmctrl click) is not routed to "
        "the compositor on GNOME 50 and asking for it pops a Remote Desktop "
        "consent dialog that grabs input until it is dismissed. Observed "
        "2026-08-16. pointer_click needs no consent."
    )
    hazard = layout_hazard()
    report["keyboard_layout"] = (
        f"{keyboard_layouts() or ['unknown']}; typing goes through compositor "
        "keysyms, which the layout cannot transpose"
        + (f" (ydotool fallback would be affected: {hazard})" if hazard else "")
    )
    try:
        a11y = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        report["toolkit_accessibility"] = a11y
    except Exception:
        report["toolkit_accessibility"] = "unknown"
    report["session_type"] = os.environ.get("XDG_SESSION_TYPE", "unset")
    report["dbus_session"] = "set" if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else "MISSING"
    return report


# =========================================================================
# MCP wiring
# =========================================================================
def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


TARGET_SCHEMA = {
    "description": "Window id from list_windows, or a wm_class / title fragment. "
                   "The window is activated and focus is CONFIRMED before any key "
                   "is sent; if focus does not land, nothing is typed.",
    "anyOf": [{"type": "integer"}, {"type": "string"}],
}

_LOOK_SCHEMA = {
    "description": (
        "What to show you afterwards. Default \"auto\": wait for the screen to "
        "stop changing, measure how much this action changed, and attach a "
        "picture of the affected window only if something did change -- so a "
        "click that hit nothing costs no tokens and says so. \"window\" always "
        "attaches it, \"screen\" uses the whole desktop (slower, 6x the tokens), "
        "\"region\" uses look_at, false skips all of it. Use false for the middle "
        "of a sequence you are going to check at the end anyway."),
    "anyOf": [{"type": "string", "enum": ["auto", "window", "screen", "region", "none"]},
              {"type": "boolean"}],
    "default": "auto",
}

_SETTLE_SCHEMA = {
    "type": "number", "default": SETTLE_MAX_S, "minimum": 0.0, "maximum": 20,
    "description": "How long to wait for the screen to stop changing before "
                   "looking. Raise it for an app that animates slowly; set it to "
                   "0 to capture immediately.",
}

_LOOK_AT_SCHEMA = {
    "type": "object",
    "description": 'Rectangle for look:"region", in screen pixels.',
    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                   "width": {"type": "integer"}, "height": {"type": "integer"}},
}

TOOLS: list[dict] = [
    {
        "name": "list_windows",
        "description": "Every open window with id, wm_class, title, geometry, pid and "
                       "which one has focus. Start here: ids from this list are what "
                       "type_text and press_keys target (ids change when a dialog is "
                       "recreated -- a wm_class or title fragment does not). "
                       "HOW TO DRIVE THIS DESKTOP, because the round trip is the "
                       "expensive part and the actions are milliseconds: (1) ui_find "
                       "then ui_press where the app has an accessibility tree -- it "
                       "cannot miss; (2) find_text for Chrome, Electron and Qt, which "
                       "expose almost nothing; (3) do_steps when you already know the "
                       "next few actions, instead of one call each; (4) let the acting "
                       "tool show you the result rather than following it with a "
                       "screenshot -- they all do now. A screenshot of the whole "
                       "screen is the last resort, not the first move.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_windows,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "screenshot",
        "description": "Look at the screen, one window, or one rectangle. The image "
                       "comes back in this reply -- there is nothing to Read "
                       "afterwards. CROP, DO NOT SHRINK: `window` costs about 1300 "
                       "tokens and a `region` strip about 160, against 1843 for the "
                       "whole desktop, and all three stay legible, while `scale` "
                       "below 1 makes small text unreadable for a saving a crop "
                       "would have made anyway. Passing `window` AND `region` means "
                       "a rectangle measured inside that window. `annotate` draws "
                       "grid lines and window boxes labelled in SCREEN coordinates, "
                       "so the number to pass to pointer_click can be read off the "
                       "picture instead of estimated. Before reaching for this at "
                       "all: ui_find and find_text answer \"where is X\" without an "
                       "image, and every acting tool already shows you the result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _s("Where to write the PNG, e.g. /tmp/shot.png"),
                "include_cursor": {"type": "boolean", "default": False},
                "window": {
                    "description": "Capture just this window (id, wm_class or title "
                                   "fragment). Captures what is ON SCREEN there, so "
                                   "anything in front of it is included.",
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
                "region": {
                    "description": "Capture just this rectangle, in screen pixels.",
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                   "width": {"type": "integer"},
                                   "height": {"type": "integer"}},
                },
                "scale": {"type": "number", "default": 1.0, "minimum": 0.05,
                          "maximum": 4.0,
                          "description": "Resize the image you are shown. Below 1 it "
                                         "shrinks -- measured on this 1920x1080 "
                                         "screen, 0.5 makes UI text hard to read and "
                                         "OCR fails on it entirely, so crop instead. "
                                         "Above 1 it enlarges, which is what a tiny "
                                         "crop of an icon needs."},
                "inline": {"type": "boolean", "default": True,
                           "description": "Set false to only write the PNG and get "
                                          "its path back, for a capture nobody needs "
                                          "to look at now."},
                "annotate": {
                    "description": "true for grid + window boxes, or an object: "
                                   "{grid: true|<spacing px>, windows: bool, "
                                   "widgets: bool, limit: int}.",
                    "anyOf": [{"type": "boolean"}, {"type": "object"}],
                },
                "app": _s("AT-SPI application name for annotate.widgets, if the "
                          "focused window is not the one to map."),
            },
            "required": ["path"],
        },
        "handler": tool_screenshot,
    },
    {
        "name": "pointer_move",
        "description": "Move the pointer to an absolute screen position. Exact: this "
                       "goes to the compositor (org.gnome.Mutter.RemoteDesktop), not "
                       "through ydotool, so there is no acceleration curve and no "
                       "closed loop needed. Coordinates are the same ones "
                       "list_windows and screen_map report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "x": {"type": "number"}, "y": {"type": "number"},
                "expect_window": {
                    "description": "Refuse the move if this window is not the one at "
                                   "that point.",
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
            },
            "required": ["x", "y"],
        },
        "handler": tool_pointer_move,
    },
    {
        "name": "pointer_click",
        "description": "Click at an absolute screen position. Reports whether it "
                       "LANDED on anything -- the screen is compared before and "
                       "after, so a click into dead space says so instead of looking "
                       "exactly like one that worked -- and shows you the result "
                       "without a separate screenshot. Also reports whether the "
                       "keyboard moved as a result. PASS expect_window: the click is "
                       "refused if something else is under that point, which is the "
                       "difference between a missed click and a click in someone "
                       "else's window. Needs no consent dialog, unlike xdotool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "x": {"type": "number"}, "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle",
                                                      "back", "forward"],
                           "default": "left"},
                "count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3,
                          "description": "2 for a double click."},
                "expect_window": {
                    "description": "The window this click is aimed at (id, wm_class "
                                   "or title fragment). Nothing is clicked if it is "
                                   "not the window at that point.",
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
            },
            "required": ["x", "y"],
        },
        "handler": tool_pointer_click,
    },
    {
        "name": "pointer_drag",
        "description": "Press at one point, travel, release at another. The travel is "
                       "real intermediate motion, because a press-and-teleport is not "
                       "a drag to most toolkits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "from_x": {"type": "number"}, "from_y": {"type": "number"},
                "to_x": {"type": "number"}, "to_y": {"type": "number"},
                "button": {"type": "string", "default": "left"},
                "steps": {"type": "integer", "default": 24},
                "expect_window": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
        "handler": tool_pointer_drag,
    },
    {
        "name": "pointer_scroll",
        "description": "Wheel clicks at a point. dy positive scrolls down, dx "
                       "positive scrolls right.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "x": {"type": "number"}, "y": {"type": "number"},
                "dy": {"type": "integer", "default": 0},
                "dx": {"type": "integer", "default": 0},
                "expect_window": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
            "required": ["x", "y"],
        },
        "handler": tool_pointer_scroll,
    },
    {
        "name": "pointer_position",
        "description": "Where the pointer is. Answers from the compositor when the "
                       "extension supports it, otherwise from the last position this "
                       "server set and says which. Never guesses from X, whose answer "
                       "is stale whenever the pointer is over a Wayland surface.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_pointer_position,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "window_at",
        "description": "What a click at this point would hit. Use it before clicking "
                       "somewhere you inferred from a screenshot. Reports both the "
                       "compositor's own pick (which respects input shapes, so a "
                       "click-through overlay is seen through) and every window whose "
                       "rectangle covers the point.",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
        },
        "handler": tool_window_at,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "screen_map",
        "description": "Everything on screen with the coordinates to reach it: the "
                       "desktop rectangle, every window top of the stack first with "
                       "its centre point, where the pointer is, and every pressable "
                       "widget of the focused application with the exact pixel to "
                       "click it at. This is the one call that turns 'click the Save "
                       "button' into a number without looking at an image.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "widgets": {"type": "boolean", "default": True},
                "app": _s("AT-SPI application name, if not the focused window's."),
                "limit": {"type": "integer", "default": 60},
            },
        },
        "handler": tool_screen_map,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wait_for",
        "description": "Wait until the desktop reaches a state, instead of sleeping a "
                       "guessed number of seconds. Conditions: window_exists, "
                       "window_gone, window_focused, focus_changes. Returns as soon as "
                       "it is true, or reports honestly that it timed out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string",
                              "enum": ["window_exists", "window_gone",
                                       "window_focused", "focus_changes"]},
                "target": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "timeout": {"type": "number", "default": 10, "minimum": 0.2,
                            "maximum": 120},
            },
            "required": ["condition"],
        },
        "handler": tool_wait_for,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "find_text",
        "description": "Where a visible piece of text is on screen, in coordinates you "
                       "can click. Reads the pixels with OCR, so it works in Chrome, "
                       "Electron and Qt apps, which expose almost nothing to ui_find. "
                       "Try ui_find FIRST -- pressing a real widget cannot miss -- and "
                       "come here when it returns nothing. About 1.5s for a window; "
                       "cheaper and more exact than taking a picture and estimating. "
                       "Blind to icon-only buttons: there is no text in them to read.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _s("The visible string to find. A phrase is matched across "
                           "consecutive words on one line."),
                "window": {
                    "description": "Search inside this window only (id, wm_class or "
                                   "title fragment). Much faster and far fewer false "
                                   "matches than the whole screen.",
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
                "region": _LOOK_AT_SCHEMA,
                "exact": {"type": "boolean", "default": False,
                          "description": "Whole word, case sensitive."},
                "min_confidence": {"type": "integer", "default": OCR_MIN_CONFIDENCE,
                                   "minimum": 0, "maximum": 100},
                "psm": {"type": "integer", "minimum": 0,
                        "maximum": 13,
                        "description": "tesseract page segmentation mode. Defaults "
                                       "to 6 inside a window and 11 for the whole "
                                       "screen, which is what measured best for each."},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["text"],
        },
        "handler": tool_find_text,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "region_changed",
        "description": "Wait until a window or rectangle CHANGES, then show it. For "
                       "anything wait_for cannot express: a reply arriving, a spinner "
                       "finishing, a download completing. Polls pixels here instead of "
                       "making you take blind screenshots and look at each one, and "
                       "returns as soon as it changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "region": _LOOK_AT_SCHEMA,
                "timeout": {"type": "number", "default": 10, "minimum": 0.2,
                            "maximum": 300},
                "poll_seconds": {"type": "number", "default": 0.3, "minimum": 0.1},
                "look": {"type": "boolean", "default": True,
                         "description": "Attach the picture once it changes."},
            },
        },
        "handler": tool_region_changed,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "do_steps",
        "description": "Run a short sequence of actions in ONE call and look once at "
                       "the end. Every separate tool call costs a model round trip of "
                       "several seconds while the action itself takes milliseconds, so "
                       "a known sequence -- activate, click, type, press Return, see "
                       "the result -- belongs here rather than in four calls. Steps run "
                       "with their own `look` off; the picture is taken after the last "
                       "one, or at the step that failed. Use single tools when the next "
                       "action depends on what the last one revealed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "maxItems": DO_STEPS_MAX,
                    "description": (
                        "Ordered actions. Each is {do: verb, ...the same arguments "
                        "that tool takes}. Verbs: activate(target), click(x,y), "
                        "move(x,y), drag(from_x,from_y,to_x,to_y), scroll(x,y,dy), "
                        "type(target,text), key(target,combo), press(path,expect_name), "
                        "set_text(path,text), wait(condition,target), sleep(ms)."),
                    "items": {"type": "object"},
                },
                "stop_on_error": {
                    "type": "boolean", "default": True,
                    "description": "Stop at the first failing step. Leave this true "
                                   "unless the steps are genuinely independent.",
                },
                "look": _LOOK_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
            },
            "required": ["steps"],
        },
        "handler": tool_do_steps,
    },
    {
        "name": "screencast",
        "description": "Record the screen, or one window, to an h264 mp4. Use this "
                       "instead of screenshot whenever the thing being judged MOVES "
                       "-- an animation, a transition, a scroll, a stutter, a hover "
                       "state. Stills cannot show motion and bursting them tops out "
                       "near 5 fps. Goes under the xdg portal straight to "
                       "org.gnome.Mutter.ScreenCast, so there is no share-your-screen "
                       "consent dialog, and encodes on the iGPU. Read the result back "
                       "by pulling frames out with ffmpeg.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _s("Where to write the mp4, e.g. /tmp/cast.mp4"),
                "seconds": {"type": "number", "default": 10, "minimum": 0.5,
                            "maximum": 120},
                "fps": {"type": "integer", "default": 30},
                "target": TARGET_SCHEMA,
                "include_cursor": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "handler": tool_screencast,
    },
    {
        "name": "frames",
        "description": "Turn a video into ONE image you can actually look at: N "
                       "frames, evenly spaced, stamped with frame number and "
                       "timestamp, tiled into a contact sheet. This is the other "
                       "half of screencast -- a model cannot decode an mp4, so a "
                       "recording is useless until it becomes stills. Also "
                       "measures per-frame change and reports duplicate frames, "
                       "which detects a source repainting slower than the capture "
                       "rate and catches stutter and frozen output that eyeballing "
                       "misses. Use from_frame/to_frame to zoom into a fraction of "
                       "a second once the overview shows where the interesting "
                       "moment is. Works on any video, not just screencast output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _s("The video to read, e.g. /tmp/cast.mp4"),
                "compare": _s("A second video. Its sheet is stacked underneath the "
                              "first in ONE image, which is what a before/after "
                              "needs -- two separate sheets are never on screen "
                              "together to be compared."),
                "outdir": _s("Where to write the sheet; defaults to "
                             "<video>-frames next to the video"),
                "cols": {"type": "integer", "default": 4, "minimum": 1,
                         "maximum": 8},
                "rows": {"type": "integer", "default": 3, "minimum": 1,
                         "maximum": 8},
                "from_frame": {"type": "integer",
                               "description": "Start of a dense slice, in frames. "
                                              "Omit to span the whole clip."},
                "to_frame": {"type": "integer"},
            },
            "required": ["path"],
        },
        "handler": tool_frames,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "activate_window",
        "description": "Focus and raise a window, then confirm focus actually landed "
                       "there. Returns an error rather than a false success.",
        "inputSchema": {"type": "object",
                        "properties": {"target": TARGET_SCHEMA,
                                       "look": _LOOK_SCHEMA,
                                       "look_at": _LOOK_AT_SCHEMA,
                                       "settle_max_s": _SETTLE_SCHEMA},
                        "required": ["target"]},
        "handler": tool_activate_window,
    },
    {
        "name": "ui_apps",
        "description": "Applications currently on the AT-SPI bus. These names are "
                       "what ui_tree and ui_find take.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_ui_apps,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ui_tree",
        "description": "Accessibility tree for one application: roles, names, screen "
                       "bounds, and which nodes are actionable. Prefer ui_find unless "
                       "you genuinely need the shape of the whole window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": _s("Application name from ui_apps"),
                "depth": {"type": "integer", "default": DEFAULT_TREE_DEPTH},
            },
            "required": ["app"],
        },
        "handler": tool_ui_tree,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ui_find",
        "description": "Find widgets by visible text. THE way to locate something to "
                       "act on: pressing a real widget through AT-SPI cannot miss and "
                       "does not care where the window moved to. Paths returned here "
                       "are valid only while the tree is unchanged -- find, then act.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _s("Substring to look for in widget names. Optional if "
                           "role or actionable_only is given -- which is how you "
                           "list the icon-only buttons that have no name to search."),
                "app": _s("Restrict to one application (much faster)"),
                "role": _s("Require an exact AT-SPI role, e.g. push_button"),
                "actionable_only": {"type": "boolean", "default": False,
                                    "description": "Only widgets that expose an action"},
                "depth": {"type": "integer", "default": DEFAULT_FIND_DEPTH,
                          "description": "GTK4 nests deeply -- the default is 30 "
                                         "because a text view can sit at depth 23"},
            },
        },
        "handler": tool_ui_find,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ui_read_text",
        "description": "Read the content of a text widget straight out of the "
                       "accessibility tree. This is how you VERIFY that something "
                       "landed, instead of trusting that a keystroke arrived.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": _s("Application name; its editable text widget is located "
                          "automatically"),
                "path": _s("Or an exact index path from ui_find"),
            },
        },
        "handler": tool_ui_read_text,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ui_set_text",
        "description": "PREFERRED way to enter text. Writes through AT-SPI "
                       "EditableText, which needs no focus and no ydotool: it works "
                       "on an unfocused window and even while the screen is locked, "
                       "and it reads the widget back to prove the text landed. Use "
                       "type_text only when a widget is not AT-SPI-editable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "text": _s("Text to write"),
                "app": _s("Application name; its editable text widget is located "
                          "automatically"),
                "path": _s("Or an exact index path from ui_find"),
                "replace": {"type": "boolean", "default": False,
                            "description": "Clear existing content first"},
            },
            "required": ["text"],
        },
        "handler": tool_ui_set_text,
    },
    {
        "name": "ui_press",
        "description": "Invoke a widget's own action through AT-SPI -- the preferred "
                       "way to act on this desktop. Requires expect_name or "
                       "expect_role, and refuses if the path no longer points at that "
                       "widget, so a shifted tree cannot make you press the wrong "
                       "thing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "path": _s("Index path from ui_find, e.g. \"gedit/0/3/1\""),
                "expect_name": _s("Name the widget should still have (substring)"),
                "expect_role": _s("Role the widget should still have"),
                "action_index": {"type": "integer", "default": 0},
            },
            "required": ["path"],
        },
        "handler": tool_ui_press,
    },
    {
        "name": "type_text",
        "description": "Type into a named window. Focus is confirmed first, nothing "
                       "is typed if it cannot be confirmed, and the widget is read "
                       "back afterwards to check the right characters arrived. "
                       "Characters go to the compositor as keysyms, so the keyboard "
                       "layout cannot transpose them -- the German-QWERTZ hazard that "
                       "made ydotool type z for y does not apply to this path. "
                       "ui_set_text is still better where it works: it hands text to "
                       "the widget and needs no focus at all.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "text": _s("Literal text to type"),
                "target": TARGET_SCHEMA,
                "key_delay_ms": {"type": "integer", "default": 20},
                "via": {"type": "string", "enum": ["auto", "keysym", "ydotool"],
                        "default": "auto",
                        "description": "auto prefers compositor keysyms and falls "
                                       "back to ydotool."},
                "verify_app": _s("AT-SPI application name to read back for "
                                 "verification; auto-detected from the window if "
                                 "omitted"),
            },
            "required": ["text", "target"],
        },
        "handler": tool_type_text,
    },
    {
        "name": "press_keys",
        "description": "Send a key combination to a named window, e.g. ctrl+s. Chain "
                       "several with do_steps rather than one call each. Focus "
                       "is confirmed first. Ctrl+Alt+F1-F12 is refused: it switches "
                       "virtual terminal and looks exactly like a frozen machine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "combo": _s("e.g. 'ctrl+shift+t'"),
                "target": TARGET_SCHEMA,
                "via": {"type": "string", "enum": ["auto", "keysym", "ydotool"],
                        "default": "auto"},
            },
            "required": ["combo", "target"],
        },
        "handler": tool_press_keys,
    },
    {
        "name": "desktop_health",
        "description": "Whether each mechanism is usable right now, and what each one "
                       "will actually do: extension state and which methods the "
                       "RUNNING shell has (an edited extension does not load until "
                       "the next login), absolute pointer control, window and AT-SPI "
                       "counts, keyboard layout, and the XTEST trap. Call this first "
                       "when something behaves oddly.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_health,
        "annotations": {"readOnlyHint": True},
    },
]

HANDLERS: dict[str, Callable[[dict], Any]] = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SCHEMAS = [
    {k: v for k, v in t.items() if k != "handler"} for t in TOOLS
]


def _content_blocks(result: Any) -> list[dict]:
    """Split a handler's return value into what the model reads and what it sees.

    Handlers stash images under `_INLINE_KEY` (and, for do_steps, a list of them
    under `_INLINE_KEY + "s"`) rather than building content blocks themselves, so
    that every tool describes itself as plain JSON and only this function knows
    the wire format.
    """
    images: list[dict] = []
    if isinstance(result, dict):
        result = dict(result)
        one = result.pop(_INLINE_KEY, None)
        many = result.pop(_INLINE_KEY + "s", None)
        if one:
            images.append(one)
        if many:
            images.extend(many)

    blocks: list[dict] = [{"type": "text", "text": json.dumps(result, indent=1)}]
    for image in images:
        blocks.append({"type": "image",
                       "source": {"type": "base64",
                                  "media_type": image["media_type"],
                                  "data": image["data"]}})
    return blocks


def _respond(msg_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                 "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        _respond(msg_id, {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {}},
                          "serverInfo": SERVER_INFO})
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _respond(msg_id, {})
        return
    if method == "tools/list":
        _respond(msg_id, {"tools": TOOL_SCHEMAS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            _error(msg_id, -32602, f"unknown tool {name!r}")
            return
        try:
            result = handler(params.get("arguments") or {})
            _respond(msg_id, {"content": _content_blocks(result)})
        except ToolError as e:
            # A tool-level failure is a result the model must see and reason
            # about, not a protocol error that hides the reason.
            _respond(msg_id, {"content": [{"type": "text", "text": str(e)}],
                              "isError": True})
        except Exception as e:
            _respond(msg_id, {"content": [{"type": "text",
                                           "text": f"{type(e).__name__}: {e}"}],
                              "isError": True})
        return
    if msg_id is not None:
        _error(msg_id, -32601, f"method not found: {method}")


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(msg)
        except Exception as e:                      # never die on one bad message
            if isinstance(msg, dict) and msg.get("id") is not None:
                _error(msg["id"], -32603, f"{type(e).__name__}: {e}")
    return 0


def self_test() -> int:
    """Prove every read-only capability from the command line."""
    checks: list[tuple[str, bool, str]] = []

    def run(label: str, fn: Callable[[], str]) -> None:
        try:
            checks.append((label, True, fn()))
        except Exception as e:
            checks.append((label, False, f"{type(e).__name__}: {e}"))

    run("health", lambda: json.dumps(tool_health({})))
    run("list_windows", lambda: f'{tool_list_windows({})["count"]} windows')
    run("ui_apps", lambda: f'{tool_ui_apps({})["count"]} apps on the bus')
    run("ui_tree(gnome-shell)",
        lambda: f'{tool_ui_tree({"app": "gnome-shell", "depth": 5})["nodes"]} nodes')
    run("screenshot", lambda: json.dumps(
        {k: v for k, v in tool_screenshot({"path": "/tmp/wcu-selftest.png"}).items()
         if k != _INLINE_KEY}))          # 200KB of base64 is not a test report
    run("ui_find(actionable)", lambda: (
        f'{tool_ui_find({"text": "/", "app": "gnome-shell", "actionable_only": True})["matches"]}'
        " actionable widgets"))
    run("KEYS table loaded", lambda: (
        f"{len(KEYS)} keys, {len(MODIFIERS)} modifiers"
        if len(KEYS) > 40 and MODIFIERS else
        (_ for _ in ()).throw(AssertionError(
            "KEYS/MODIFIERS failed to import from desktop.py -- press_keys is dead "
            "and every guard below would pass vacuously"))))
    run("VT-switch guard", lambda: (
        "ctrl+alt+f2 refused as switch-to-session"
        if _expect_refusal("ctrl+alt+f2", "switch-to-session") else
        (_ for _ in ()).throw(AssertionError("ctrl+alt+f2 not refused for the right reason"))))
    run("logout-combo guard", lambda: (
        "ctrl+alt+delete refused as logout"
        if _expect_refusal("ctrl+alt+delete", "bound to logout") else
        (_ for _ in ()).throw(AssertionError("ctrl+alt+delete not refused for the right reason"))))
    run("focus guard", lambda: (
        "type_text without target refused"
        if _expect_tool_error(tool_type_text, {"text": "x"}) else
        (_ for _ in ()).throw(AssertionError("typing without a target was allowed"))))
    run("identity guard", lambda: (
        "ui_press without expectation refused"
        if _expect_tool_error(tool_ui_press, {"path": "gnome-shell/0"}) else
        (_ for _ in ()).throw(AssertionError("ui_press without expectation allowed"))))
    run("pointer space", lambda: (
        "desktop is {}x{} at ({}, {})".format(
            *(lambda b: (b[2], b[3], b[0], b[1]))(_pointer().desktop_bounds()))))
    # Not knowing where the pointer is, and saying so, is a correct state until
    # the extension gains Pointer at the next login. Only a wrong answer is a
    # failure here.
    run("pointer position", lambda: json.dumps(_position_or_reason()))
    run("window_at centre", lambda: (
        lambda b: json.dumps(
            {k: v for k, v in window_at(b[0] + b[2] / 2, b[1] + b[3] / 2).items()
             if k in ("window", "source")}))(_pointer().desktop_bounds()))
    run("off-screen click refused", lambda: (
        "a click outside the desktop is refused"
        if _expect_tool_error(tool_pointer_move, {"x": -50, "y": -50}) else
        (_ for _ in ()).throw(AssertionError("moved the pointer off the desktop"))))
    run("expect_window guard", lambda: (
        "a click naming the wrong window is refused"
        if _expect_tool_error(
            tool_pointer_click,
            {"x": 0, "y": 0, "expect_window": "no-such-window-anywhere"}) else
        (_ for _ in ()).throw(AssertionError("clicked while naming a missing window"))))
    run("keysym table", lambda: (
        lambda ri: f"{len(ri.KEYSYMS)} keysyms, 'y' is {hex(ri.char_to_keysym('y'))}"
        if ri.char_to_keysym("y") == ord("y") else
        (_ for _ in ()).throw(AssertionError("char_to_keysym is wrong for ASCII"))
    )(_input()))
    run("extension methods", lambda: ", ".join(sorted(extension_methods())) or "none")

    failed = 0
    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label:<24} {detail[:150]}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


def _position_or_reason() -> dict:
    try:
        return pointer_position()
    except ToolError as e:
        return {"known": False, "why": str(e)[:120]}


def _expect_refusal(combo: str, because: str) -> bool:
    """Refused for the RIGHT reason.

    Without checking the message this passed vacuously: if the KEYS import at the
    top failed, KEYS is empty, every combo is refused as "unknown key 'ctrl'", and
    the self-test printed PASS for a VT guard it had never reached -- while
    press_keys was entirely dead.
    """
    try:
        parse_combo(combo)
        return False
    except ToolError as e:
        return because in str(e)


def _expect_tool_error(fn: Callable[[dict], Any], args: dict) -> bool:
    try:
        fn(args)
        return False
    except ToolError:
        return True


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(serve())
