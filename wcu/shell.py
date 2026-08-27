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

# Two extensions can serve this server. The bundled one (extension/ in this
# repo) is preferred; the legacy migration-helpers bus keeps everything
# working on a machine that has not loaded it yet. The pick happens once per
# process, on first use -- gnome-shell cannot gain or lose an extension
# without a re-login anyway, so probing more often buys nothing.
NEW_BUS = "org.wcu.Helpers"
NEW_PATH = "/org/wcu/Helpers"
NEW_UUID = "wcu@wayland-computer-use"
OLD_BUS = "org.tristan.MigrationHelpers"
OLD_PATH = "/org/tristan/MigrationHelpers"
OLD_UUID = "migration-helpers@tristan.local"

BUS_NAME = OLD_BUS
OBJ_PATH = OLD_PATH
EXTENSION_UUID = OLD_UUID
_BUS_PROBED = False

FOCUS_TIMEOUT_S = 3.0
FOCUS_POLL_S = 0.1


def _introspect_methods(bus: str, path: str) -> set[str]:
    try:
        xml = subprocess.run(
            ["gdbus", "introspect", "--session", "--dest", bus,
             "--object-path", path, "--xml"],
            capture_output=True, text=True, timeout=15).stdout
        return set(re.findall(r'<method name="([^"]+)"', xml))
    except Exception:
        return set()


def _pick_bus() -> None:
    """Prefer the bundled extension's bus; fall back to migration-helpers."""
    global BUS_NAME, OBJ_PATH, EXTENSION_UUID, _BUS_PROBED, _EXTENSION_METHODS
    if _BUS_PROBED:
        return
    _BUS_PROBED = True
    methods = _introspect_methods(NEW_BUS, NEW_PATH)
    if methods:
        BUS_NAME, OBJ_PATH, EXTENSION_UUID = NEW_BUS, NEW_PATH, NEW_UUID
        _EXTENSION_METHODS = methods


# =========================================================================
# transport-independent capability layer
# =========================================================================
def _gdbus(method: str, *args: str, timeout: float = 30.0) -> str:
    _pick_bus()
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
    _pick_bus()
    if _EXTENSION_METHODS is None:
        _EXTENSION_METHODS = _introspect_methods(BUS_NAME, OBJ_PATH)
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
        return _disambiguate(target, matches)
    return matches[0]


# A window smaller than this is not what anyone meant by "the Acrobat window".
# Acrobat spawns a hidden 1x1 "Welcome" dialog while it launches (2026-08-26),
# and Chrome and Electron apps keep similar zero-size helpers around.
_DEGENERATE_AREA = 100 * 100


def _disambiguate(target: Any, matches: list[dict]) -> dict:
    """Pick THE window out of several matches, or refuse if there is no
    defensible pick.

    Refusing outright was the old behaviour and it was wrong in the common
    case: window ids churn (an app opens a splash, a hidden helper, a modal),
    so the id the error demands is exactly what the caller could not know,
    and every app launch cost an extra `list_windows` round trip
    (2026-08-25, 2026-08-26).

    But silently picking one is how input lands in the wrong window, which is
    the failure this codebase spends the most guards on. So the pick has to
    be defensible on its own terms -- focus, or window type, or a size
    difference nobody would argue with -- and it is always REPORTED, never
    silent.
    """
    def _pick(candidates: list[dict], why: str) -> dict | None:
        if len(candidates) != 1:
            return None
        chosen = dict(candidates[0])
        chosen["disambiguated"] = {
            "why": why,
            "passed_over": [{"id": w["id"], "title": w["title"],
                             "type": w.get("type"),
                             "size": f'{w.get("width")}x{w.get("height")}'}
                            for w in matches if w["id"] != candidates[0]["id"]][:5],
        }
        return chosen

    real = [w for w in matches if not w.get("minimized")]
    for candidates, why in (
        ([w for w in matches if w.get("focused")],
         "it is the focused one"),
        ([w for w in real if (w.get("type") or "NORMAL") == "NORMAL"
          and w.get("width", 0) * w.get("height", 0) >= _DEGENERATE_AREA],
         "it is the only normal-sized application window; the rest are "
         "dialogs, splashes or zero-size helpers"),
        ([w for w in real if w.get("width", 0) * w.get("height", 0)
          >= _DEGENERATE_AREA],
         "it is the only one big enough to be a real window"),
    ):
        chosen = _pick(candidates, why)
        if chosen:
            return chosen

    raise ToolError(
        f"{target!r} matches {len(matches)} windows and none of them is the "
        "obvious one (no single focused window, and more than one normal "
        "window of a usable size), so picking for you could put input in the "
        "wrong one. Pass an id: "
        + ", ".join(f'{w["id"]} {w["title"]!r} '
                    f'[{w.get("type", "?")} {w.get("width")}x{w.get("height")}]'
                    for w in matches),
        code="bad_args",
    )


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


WAIT_CONDITIONS = {"window_focused", "window_exists", "window_gone",
                   "focus_changes", "text_appears", "widget_exists",
                   "clipboard_changed", "elapsed"}

# A wait can legitimately be long -- an app launch, an installer, a first-run
# index. The old 120s hard error did not stop anyone waiting longer, it just
# made them chain two calls or fake it with a text_appears that could never
# match (2026-08-25, 2026-08-26); 300s covers the 150-180s that was actually
# asked for, with headroom.
#
# Not higher, deliberately: this blocks a single MCP call, and a wait that
# outlives the client's own per-call timeout fails in a way the agent cannot
# tell from a broken tool. Past this the honest thing is two calls, which
# `elapsed` now makes cheap.
WAIT_TIMEOUT_MAX_S = 300.0
WAIT_TIMEOUT_MIN_S = 0.2


def _probe_text_appears(text: str, target: Any) -> tuple[bool, str]:
    """One OCR pass over the target window: is the string visible yet?"""
    from .ocr import tool_find_text     # late: ocr imports capture imports shell
    try:
        found = tool_find_text({"text": text, "window": target, "limit": 1})
    except ToolError as e:
        return False, f"not readable yet: {e}"
    n = found.get("matches", 0)
    return bool(n), (f"{text!r} visible" if n else f"{text!r} not on screen")


def _probe_widget_exists(app: Any, text: Any, role: Any) -> tuple[bool, str]:
    from .atspi import tool_ui_find     # late: atspi imports capture imports shell
    query = {"app": app}
    if text:
        query["text"] = text
    if role:
        query["role"] = role
    try:
        found = tool_ui_find(query)
    except ToolError as e:
        return False, f"not on the bus yet: {e}"
    n = found.get("matches", 0)
    return bool(n), (f"{n} widget(s) match" if n else "no widget matches")


def _read_clipboard_now() -> str | None:
    from .input import tool_clipboard_read      # late: input imports shell
    try:
        return tool_clipboard_read({}).get("text")
    except ToolError:
        return None


def tool_wait_for(a: dict) -> dict:
    """Poll a desktop condition instead of sleeping and hoping."""
    requested = float(a.get("timeout") or 10)
    if requested < WAIT_TIMEOUT_MIN_S:
        raise ToolError(f"timeout must be at least {WAIT_TIMEOUT_MIN_S} seconds",
                        code="bad_args")
    # Clamp rather than refuse. A rejected call taught callers to chain two
    # waits or drop to a shell loop; a clamped one waits as long as it can
    # and SAYS so, which is the same information without the round trip.
    timeout = min(requested, WAIT_TIMEOUT_MAX_S)
    clamped = requested > timeout
    condition = str(a.get("condition") or "").strip()
    target = a.get("target")
    if condition not in WAIT_CONDITIONS:
        raise ToolError(
            f"condition must be one of: {', '.join(sorted(WAIT_CONDITIONS))}",
            code="bad_args")

    # A plain duration. It exists because there was no honest way to say
    # "wait 120s" -- `sleep` lives only inside do_steps and foreground shell
    # sleeps are blocked, so an agent polling a long install used
    # `text_appears` on a string it knew could never appear, ~10 times in one
    # session (2026-08-26). That is a lie in the transcript and a wasted OCR
    # pass every 0.4s; this is the same wait, told truthfully and cheaply.
    if condition == "elapsed":
        start = time.monotonic()
        time.sleep(timeout)
        waited = round(time.monotonic() - start, 2)
        out = {"condition": condition, "met": True, "waited_seconds": waited,
               "evidence": f"waited {waited}s; nothing was polled and nothing "
                           "was changed by waiting"}
        if clamped:
            out["clamped_from"] = requested
        return out

    if condition in ("window_focused", "window_exists", "window_gone",
                     "text_appears") and target is None:
        raise ToolError(f"{condition} needs a target window", code="bad_args")
    if condition == "text_appears" and not a.get("text"):
        raise ToolError("text_appears needs text to look for", code="bad_args")
    if condition == "widget_exists" and not a.get("app"):
        raise ToolError("widget_exists needs app (and text and/or role)",
                        code="bad_args")
    if condition == "widget_exists" and not (a.get("text") or a.get("role")):
        raise ToolError("widget_exists needs text and/or role", code="bad_args")

    clamp_note = {"clamped_from": requested} if clamped else {}

    # The slow-probe conditions poll on their own rhythm: an OCR pass is
    # ~0.3s of work, so re-running it every 0.15s would be pure heat.
    if condition in ("text_appears", "widget_exists", "clipboard_changed"):
        start = time.monotonic()
        clip_before = _read_clipboard_now() if condition == "clipboard_changed" else None
        while True:
            if condition == "text_appears":
                met, evidence = _probe_text_appears(str(a["text"]), target)
            elif condition == "widget_exists":
                met, evidence = _probe_widget_exists(a["app"], a.get("text"),
                                                     a.get("role"))
            else:
                now = _read_clipboard_now()
                met = now != clip_before
                evidence = ("clipboard changed" if met
                            else "clipboard still holds the same content")
            waited = round(time.monotonic() - start, 2)
            if met:
                return {"condition": condition, "met": True,
                        "waited_seconds": waited, "evidence": evidence,
                        **clamp_note}
            if waited >= timeout:
                return {"condition": condition, "met": False,
                        "waited_seconds": waited, "evidence": evidence,
                        "detail": "timed out; nothing was changed by waiting",
                        **clamp_note}
            time.sleep(0.4)

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
                                 "title": w["title"]} for w in hits[:5]],
                    **clamp_note}
        if waited >= timeout:
            return {"condition": condition, "met": False, "waited_seconds": waited,
                    "focused": (focused[0]["wm_class"] if focused else None),
                    "detail": "timed out; nothing was changed by waiting",
                    **clamp_note}
        time.sleep(0.15)


def tool_assert_state(a: dict) -> dict:
    """Prove the desktop is in a state, with evidence, instead of assuming it.

    Ends a task the honest way: each assertion is evaluated once and comes
    back passed/failed with what was actually observed. A failed assertion is
    a RESULT, not an error -- the caller decides what a false answer means,
    which is what makes this usable as the last step of an autonomous run.
    """
    checks: list[dict] = []

    def record(name: str, passed: bool, evidence: str) -> None:
        checks.append({"check": name, "passed": bool(passed),
                       "evidence": evidence})

    if a.get("window_exists") is not None:
        try:
            w = _resolve_target(a["window_exists"])
            record("window_exists", True,
                   f'{w["wm_class"]} {w["title"]!r} (id {w["id"]})')
        except ToolError as e:
            record("window_exists", False, str(e)[:160])

    if a.get("window_focused") is not None:
        try:
            w = _resolve_target(a["window_focused"])
            record("window_focused", bool(w.get("focused")),
                   f'{w["wm_class"]} focused={w.get("focused")}')
        except ToolError as e:
            record("window_focused", False, str(e)[:160])

    if a.get("text_present") is not None:
        spec = a["text_present"]
        if not isinstance(spec, dict) or not spec.get("text") \
                or spec.get("window") is None:
            raise ToolError('text_present must be {"text": ..., "window": ...}',
                            code="bad_args")
        met, evidence = _probe_text_appears(str(spec["text"]), spec["window"])
        record("text_present", met, evidence)

    if a.get("widget_exists") is not None:
        spec = a["widget_exists"]
        if not isinstance(spec, dict) or not spec.get("app") \
                or not (spec.get("text") or spec.get("role")):
            raise ToolError('widget_exists must be {"app": ..., and "text" '
                            'and/or "role"}', code="bad_args")
        met, evidence = _probe_widget_exists(spec["app"], spec.get("text"),
                                             spec.get("role"))
        record("widget_exists", met, evidence)

    if a.get("clipboard_contains") is not None:
        needle = str(a["clipboard_contains"])
        now = _read_clipboard_now()
        met = now is not None and needle in now
        record("clipboard_contains", met,
               ("clipboard is empty/unreadable" if now is None else
                f"clipboard holds {len(now)} chars, needle "
                + ("found" if met else "absent")))

    if not checks:
        raise ToolError(
            "nothing to assert: give window_exists, window_focused, "
            "text_present, widget_exists, and/or clipboard_contains",
            code="bad_args")

    passed = all(c["passed"] for c in checks)
    return {"passed": passed,
            "checks": checks,
            "detail": (f'{sum(c["passed"] for c in checks)}/{len(checks)} '
                       "assertions hold")}


def halt_active() -> bool:
    """Whether the human kill switch is engaged.

    Only the bundled extension has it; on a shell that has not loaded it yet
    this is a cached no at zero cost. Any probe failure counts as not-halted:
    the switch exists to let a human stop the server, never to let a D-Bus
    hiccup stop it.
    """
    if "HaltActive" not in extension_methods():
        return False
    try:
        return "true" in _gdbus("HaltActive", timeout=5.0).lower()
    except ToolError:
        return False


_WINDOW_VERBS = {
    "move_resize": ("MoveResize", ("x", "y", "width", "height")),
    "close":       ("Close", ()),
    "minimize":    ("Minimize", ()),
    "unminimize":  ("Unminimize", ()),
    "maximize":    ("Maximize", ()),
    "unmaximize":  ("Maximize", ()),
    "workspace":   ("SetWorkspace", ("index",)),
    "above":       ("SetAbove", ()),
}


def tool_window_manage(a: dict) -> dict:
    """Move, resize, close, (un)minimize, (un)maximize, re-workspace or pin a
    window -- through the compositor, where these are ordinary calls."""
    action = str(a.get("action") or "").strip()
    if action not in _WINDOW_VERBS:
        raise ToolError("action must be one of: "
                        + ", ".join(sorted(_WINDOW_VERBS)), code="bad_args")
    if a.get("target") is None:
        raise ToolError("target is required (window id, wm_class or title "
                        "fragment)", code="bad_args")
    method, needs = _WINDOW_VERBS[action]
    missing = [k for k in needs if a.get(k) is None]
    if missing:
        raise ToolError(f"{action} needs {', '.join(missing)}",
                        code="bad_args")
    if method not in extension_methods():
        raise ToolError(_needs_relogin(method), code="needs_relogin")

    window = _resolve_target(a["target"])
    wid = window["id"]
    args: list[str] = [str(wid)]
    if action == "move_resize":
        args += [str(int(a[k])) for k in ("x", "y", "width", "height")]
    elif action == "workspace":
        args += [str(int(a["index"]))]
    elif action in ("maximize", "unmaximize"):
        args += ["true" if action == "maximize" else "false"]
    elif action == "above":
        args += ["true" if a.get("above", True) else "false"]
    out = _gdbus(method, *args)
    if "true" not in out.lower():
        raise ToolError(
            f"{action} on {window['wm_class']} (id {wid}) was refused by the "
            "compositor -- the id may have gone stale, or the window does not "
            "support it (a non-resizable dialog, an out-of-range workspace)",
            code="window_not_found",
        )

    # Report the world as it IS afterwards, not the call as it was sent --
    # and poll for it rather than guessing a settle: measured 2026-08-23, a
    # MoveResize is applied but still reports the OLD geometry at 0.3s and
    # the new one by 1.5s.
    before_geo = {k: window[k] for k in ("x", "y", "width", "height")}
    deadline = time.monotonic() + 2.0
    now = None
    while time.monotonic() < deadline:
        time.sleep(0.25)
        now = next((w for w in list_windows() if w["id"] == wid), None)
        if action == "close" and now is None:
            break
        if now is not None:
            changed = {k: now[k] for k in before_geo} != before_geo
            state_done = ((action == "minimize" and now.get("minimized"))
                          or (action == "unminimize" and not now.get("minimized"))
                          or (action == "above" and bool(now.get("above"))
                              == bool(a.get("above", True))))
            if action in ("move_resize", "maximize", "unmaximize",
                          "workspace") and changed:
                break
            if state_done:
                break
    result: dict[str, Any] = {"action": action, "id": wid,
                              "wm_class": window["wm_class"]}
    if action == "close":
        result["closed"] = now is None
        result["detail"] = ("window is gone" if now is None else
                            "close was accepted but the window is still here "
                            "-- an unsaved-changes dialog is the usual reason")
    elif now is None:
        result["detail"] = "the window vanished after the action"
    else:
        result["window"] = now
        result["detail"] = (f'{action} done; geometry now '
                            f'{now["width"]}x{now["height"]}+{now["x"]}+{now["y"]}'
                            + (", minimized" if now.get("minimized") else "")
                            + (", above" if now.get("above") else ""))
    return result
