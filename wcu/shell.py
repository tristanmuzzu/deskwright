from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import ToolError

BUS_NAME = "org.tristan.MigrationHelpers"
OBJ_PATH = "/org/tristan/MigrationHelpers"
EXTENSION_UUID = "migration-helpers@tristan.local"

FOCUS_TIMEOUT_S = 3.0
FOCUS_POLL_S = 0.1


# =========================================================================
# transport-independent capability layer
# =========================================================================
def _gdbus(method: str, *args: str, timeout: float = 30.0) -> str:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise ToolError(
            "DBUS_SESSION_BUS_ADDRESS is not set, so the session bus is "
            "unreachable. This server has to run inside Tristan's graphical "
            "session -- it cannot work over a bare ssh login or from a system "
            "service.",
            code="extension_unavailable",
        )
    cmd = ["gdbus", "call", "--session", "--dest", BUS_NAME,
           "--object-path", OBJ_PATH, "--method", f"{BUS_NAME}.{method}", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{method} did not answer within {timeout:.0f}s",
                        code="extension_unavailable") from None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "ServiceUnknown" in err or "was not provided" in err or "NoReply" in err:
            raise ToolError(
                f"the {EXTENSION_UUID} extension is not answering on D-Bus.\n"
                f"  {_extension_diagnosis()}",
                code="extension_unavailable",
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
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}",
                        code="extension_unavailable") from exc
    if not (isinstance(value, tuple) and len(value) == 1 and isinstance(value[0], str)):
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}",
                        code="extension_unavailable")
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
        raise ToolError("no windows are open, so there is nothing to target",
                        code="window_not_found")

    if isinstance(target, bool):
        raise ToolError("target must be a window id or a name, not a boolean",
                        code="bad_args")

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
            + ", ".join(f'{w["id"]} {w["wm_class"]!r} {w["title"]!r}' for w in windows),
            code="window_not_found",
        )

    if not isinstance(target, str) or not target.strip():
        raise ToolError("target must be a window id, a wm_class, or a title fragment",
                        code="bad_args")

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
            + ", ".join(f'{w["id"]} {w["wm_class"]!r} {w["title"]!r}' for w in windows),
            code="window_not_found",
        )
    if len(matches) > 1:
        raise ToolError(
            f"{target!r} matches {len(matches)} windows; pass an id instead: "
            + ", ".join(f'{w["id"]} {w["title"]!r}' for w in matches),
            code="bad_args",
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
        "usually minimized on another workspace, or a modal dialog owns the focus.",
        code="focus_not_acquired",
    )


def _point(a: dict, xk: str = "x", yk: str = "y") -> tuple[float, float]:
    for key in (xk, yk):
        if a.get(key) is None:
            raise ToolError(f"{xk} and {yk} are required, in screen pixels",
                            code="bad_args")
        if isinstance(a[key], bool) or not isinstance(a[key], (int, float)):
            raise ToolError(f"{key} must be a number, got {a[key]!r}",
                            code="bad_args")
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


# =========================================================================
# tools
# =========================================================================
def tool_list_windows(_: dict) -> dict:
    windows = list_windows()
    return {"count": len(windows), "windows": windows}


def tool_activate_window(a: dict) -> dict:
    from .capture import _look, _look_before  # late: capture imports this module
    watching = _look_before(a)
    result = focus_window(a.get("target"))
    if watching.mode is not False:
        # Look at the window that was just raised, not whatever was in front
        # when the call started.
        watching.window = result["window"]
        watching.region = (result["window"]["x"], result["window"]["y"],
                           result["window"]["width"], result["window"]["height"])
    return _look(a, result, watching)


def tool_window_at(a: dict) -> dict:
    x, y = _point(a)
    return window_at(x, y)


def tool_wait_for(a: dict) -> dict:
    """Poll a desktop condition instead of sleeping and hoping."""
    timeout = float(a.get("timeout") or 10)
    if not 0.2 <= timeout <= 120:
        raise ToolError("timeout must be between 0.2 and 120 seconds",
                        code="bad_args")
    condition = str(a.get("condition") or "").strip()
    target = a.get("target")
    known = {"window_focused", "window_exists", "window_gone", "focus_changes"}
    if condition not in known:
        raise ToolError(f"condition must be one of: {', '.join(sorted(known))}",
                        code="bad_args")
    if condition != "focus_changes" and target is None:
        raise ToolError(f"{condition} needs a target window", code="bad_args")

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
