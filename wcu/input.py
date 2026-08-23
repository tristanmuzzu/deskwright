from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .atspi import (
    REFS_CONTRACT,
    _atspi_app_for_window,
    _clickable_widgets,
    _find_text_widget,
    _read_text,
    _window_for_atspi_app,
    ensure_widget_focus,
    register_refs,
    resolve_ref,
)
from .capture import _Look, _look, _look_before, _look_typed
from .config import KEYS, MODIFIERS
from .errors import ToolError
from .shell import (
    _gdbus,
    _needs_relogin,
    _point,
    _resolve_target,
    _unwrap_gvariant_string,
    extension_methods,
    focus_window,
    list_windows,
    window_at,
)

YDOTOOL_SOCKET = "/run/ydotoold.socket"


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
        raise ToolError("ydotool is not installed, so no input can be injected",
                        code="input_backend_failed")
    if not os.path.exists(YDOTOOL_SOCKET):
        raise ToolError(
            f"{YDOTOOL_SOCKET} is missing -- ydotoold is not running. It must be a "
            "SYSTEM service (systemctl status ydotoold); a user service cannot open "
            "/dev/uinput when the session predates the groupadd.",
            code="input_backend_failed",
        )
    env = dict(os.environ, YDOTOOL_SOCKET=YDOTOOL_SOCKET)
    try:
        proc = subprocess.run(["ydotool", *args], env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"ydotool did not finish within {timeout:.0f}s",
                        code="input_backend_failed") from None
    if proc.returncode != 0:
        raise ToolError(f"ydotool failed: {(proc.stderr or '').strip()[:200]}",
                        code="input_backend_failed")


def parse_combo(combo: str) -> list[int]:
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ToolError("empty key combination", code="bad_args")

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
            "power-off. There is no flag to override this.",
            code="refused_combo",
        )
    # The human halt combo may never be injected. The extension's 2s debounce
    # already blocks the instant-double-press; this guard closes the other
    # route -- an agent clearing its own halt through this server. A different
    # client could still inject it, which is stated honestly in the extension
    # source; this server at least is not that client.
    has_super = bool({"super", "meta", "leftmeta"} & mods)
    if has_super and has_ctrl and "escape" in set(parts) - mods:
        raise ToolError(
            f"refusing to inject {combo!r}. Super+Ctrl+Escape is the human halt "
            "switch: it exists so a person can stop this server, and a server "
            "that can press it can un-press it. There is no flag to override "
            "this.",
            code="refused_combo",
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
            "to override this.",
            code="refused_combo",
        )
    codes = []
    for part in parts:
        if part not in KEYS:
            raise ToolError(
                f"unknown key {part!r}. Known: {', '.join(sorted(KEYS)[:24])}, ...",
                code="bad_args",
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
            raise ToolError(f"no keysym known for {part!r}",
                            code="input_backend_failed")
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
            "It needs PyGObject (python3-gi) and a session bus.",
            code="input_backend_failed",
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
                    raise ToolError(str(e), code="input_backend_failed") from None
                raise
        return wrapped


def _pointer() -> Any:
    return _InputProxy(_input().shared())


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
        + _needs_relogin("Pointer"),
        code="needs_relogin",
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
            "that point. Nothing was clicked.",
            code="occluded",
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
        f"the window actually is.",
        code="occluded",
    )


def tool_type_text(a: dict) -> dict:
    text = a.get("text")
    if not isinstance(text, str) or text == "":
        raise ToolError("text is required", code="bad_args")
    target = a.get("target")
    if target is None:
        raise ToolError(
            "target is required. ydotool injection is focus-blind, so typing "
            "without naming a window means typing into whatever is in front. Pass a "
            "window id or wm_class from list_windows.",
            code="no_expectation",
        )
    focus = focus_window(target)
    delay = int(a.get("key_delay_ms") or 20)
    watching = _look_before(a, hint_window=focus["window"])

    # Read the widget BEFORE typing so we can tell what this call actually added.
    app_hint = a.get("verify_app") or _atspi_app_for_window(focus["window"])
    before = None
    widget_focus = None
    if app_hint:
        # Window focus is not widget focus: after an AT-SPI popover dance the
        # focused window can have NO widget holding the keyboard, and keysyms
        # then land in a void (measured 2026-08-23, gnome-text-editor). Grab
        # focus onto the preferred text widget before typing, and read THAT
        # widget back afterwards so write and verify address one document.
        widget_focus = ensure_widget_focus(str(app_hint), a.get("path"))
        if widget_focus["state"] in ("grab_failed",):
            # GTK4 refuses AT-SPI GrabFocus (atspi_error 1, measured
            # 2026-08-23) and gives no usable screen extents to click, so
            # the remaining route is the one a keyboard user has: Tab until
            # the text widget reports FOCUSED. Two tabs recovered the
            # measured popover case; eight is the give-up budget.
            syms = combo_keysyms("tab")
            for _ in range(8):
                _pointer().combo(syms)
                time.sleep(0.25)
                widget_focus = ensure_widget_focus(str(app_hint),
                                                   widget_focus.get("path"))
                if widget_focus["state"] == "already":
                    widget_focus = {"state": "tabbed",
                                    "path": widget_focus["path"]}
                    break
        try:
            before = _read_text(_find_text_widget(str(app_hint),
                                                  widget_focus.get("path")))
        except ToolError:
            before = None

    # Keysyms first: the compositor is handed the CHARACTER, so the active XKB
    # layout cannot transpose it. ydotool is the fallback, and the reason this
    # function still has a verification pass at all.
    via = str(a.get("via") or "auto").lower()
    if via not in ("auto", "keysym", "ydotool"):
        raise ToolError("via must be auto, keysym or ydotool", code="bad_args")
    used = "ydotool"
    if via in ("auto", "keysym"):
        try:
            _pointer().type_text(text, delay=max(delay, 8) / 1000)
            used = "compositor keysyms"
        except Exception as e:
            if via == "keysym":
                raise ToolError(f"keysym typing failed: {e}",
                                code="input_backend_failed") from None
            _ydotool("type", "--key-delay", str(delay), text,
                     timeout=max(30.0, len(text) * delay / 1000 + 15))
    else:
        _ydotool("type", "--key-delay", str(delay), text,
                 timeout=max(30.0, len(text) * delay / 1000 + 15))

    result = {"characters": len(text), "focus": focus["detail"], "via": used,
              "detail": f'sent {len(text)} characters to {focus["window"]["wm_class"]}'}
    if widget_focus and widget_focus["state"] != "already":
        result["widget_focus"] = widget_focus["state"]
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
        after = _read_text(_find_text_widget(
            str(app_hint), widget_focus.get("path") if widget_focus else None))
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
        "transposed.",
        code="input_backend_failed",
    )


def tool_press_keys(a: dict) -> dict:
    combo = a.get("combo")
    if not combo:
        raise ToolError("combo is required, e.g. 'ctrl+s'", code="bad_args")
    target = a.get("target")
    if target is None:
        raise ToolError(
            "target is required. Key injection is focus-blind; a combo sent at the "
            "wrong window can do real damage. Pass a window id or wm_class.",
            code="no_expectation",
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
                raise ToolError(f"keysym combo failed: {e}",
                                code="input_backend_failed") from None
            used = "ydotool"
    if used == "ydotool":
        sequence = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        _ydotool("key", "--key-delay", "40", *sequence)
    result = {"combo": combo, "focus": focus["detail"], "via": used,
              "detail": f'sent {combo} to {focus["window"]["wm_class"]}'}
    return _look(a, result, watching)


HOLD_KEY_MAX_S = 300.0


def tool_hold_key(a: dict) -> dict:
    """Hold ONE key down for a duration, then release it.

    press_keys taps; games, repeat-scrolling a list, and shift-selecting a
    range all need a real held key. The press and the release are separate
    NotifyKeyboardKeysym events with a sleep between them, so the compositor
    sees exactly what a finger on the key looks like.
    """
    key = a.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ToolError("key is required, e.g. 'shift', 'down' or 'w'",
                        code="bad_args")
    if "+" in key:
        raise ToolError(
            "hold_key holds ONE key. A combination like 'ctrl+c' is a tap, "
            "which is press_keys' job.",
            code="bad_args",
        )
    seconds = a.get("seconds")
    if seconds is None:
        raise ToolError("seconds is required: how long to hold the key. This "
                        "call blocks for the whole duration.", code="bad_args")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ToolError(f"seconds must be a number, got {seconds!r}",
                        code="bad_args")
    seconds = float(seconds)
    if not 0.05 <= seconds <= HOLD_KEY_MAX_S:
        raise ToolError(
            f"seconds must be between 0.05 and {HOLD_KEY_MAX_S:.0f}",
            code="bad_args",
        )
    syms = combo_keysyms(key)           # unknown-key errors before any focus theft
    target = a.get("target")
    focus = focus_window(target) if target is not None else None
    watching = _look_before(a, hint_window=focus["window"] if focus else None)

    # A dedicated session rather than the shared one, and the difference is not
    # style: the shared session stops itself after 25s idle, and mutter releases
    # every key a session still holds when that session ends -- so a 60-second
    # hold on the shared session would be silently released at second 25 with
    # nothing reporting it. This session is told to outlive the hold, and is
    # stopped explicitly the moment the key is released.
    ri = _InputProxy(_input().RemoteInput(idle_timeout=seconds + 30.0))
    started = time.monotonic()
    ri.keysym(syms[0], True)
    try:
        time.sleep(seconds)
    finally:
        try:
            ri.keysym(syms[0], False)
        finally:
            ri.stop()

    held = round(time.monotonic() - started, 2)
    result: dict[str, Any] = {
        "key": key.strip().lower(), "held_seconds": held,
        "detail": f"held {key.strip().lower()} for {held}s "
                  "(compositor keysym press, sleep, release)",
    }
    if focus:
        result["focus"] = focus["detail"]
    else:
        result["note"] = ("no target named, so the key went to whichever window "
                          "already had focus")
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
    button = str(a.get("button") or "left")
    count = int(a.get("count") or 1)
    if not 1 <= count <= 3:
        raise ToolError("count must be 1, 2 or 3", code="bad_args")
    ref = a.get("ref")
    resolved = None
    if ref is not None:
        if a.get("x") is not None or a.get("y") is not None:
            raise ToolError(
                "give ref OR x/y, not both. A ref already knows where its widget "
                "is; coordinates alongside it mean one of the two is wrong.",
                code="bad_args",
            )
        # Identity is re-verified and the click point recomputed from the
        # widget's CURRENT extents -- the table's stored point is where the
        # widget was at screen_map time, not necessarily where it is now.
        resolved = resolve_ref(ref)
        x, y = resolved["click_at"]
        if a.get("expect_window") is None and resolved["window_id"] is not None:
            # The table knows which window the widget lived in, so the click
            # gets the same misdirection guard an explicit expect_window buys.
            a = dict(a, expect_window=resolved["window_id"])
    else:
        x, y = _point(a)
    guard = _guard_point(x, y, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x, y))
    before = [w for w in list_windows() if w.get("focused")]
    _pointer().click(x, y, button=button, count=count)

    result = _pointer_result(x, y, f"{button} click x{count}", guard)
    if resolved is not None:
        result["ref"] = ref
        result["widget"] = (f'{resolved["name"] or resolved["path"]} '
                            f'[{resolved["role"]}]')
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
    before = [w for w in list_windows() if w.get("focused")]
    _pointer().drag(x1, y1, x2, y2, button=str(a.get("button") or "left"),
                    steps=int(a.get("steps") or 24))

    result = _pointer_result(x2, y2, "drag", guard)
    del result["x"], result["y"]
    result["from"], result["to"] = [x1, y1], [x2, y2]
    # The same flow as pointer_click, because a drag used to report success
    # blind: the release happened, so it "worked", whether or not the drop
    # landed on anything. The before/after comparison in _look is what turns
    # that into a stated verdict, and the focus read-out says whether the
    # keyboard moved -- a drop that opened something usually moves it.
    if watching.mode is False:
        time.sleep(0.25)
    else:
        _look(a, result, watching)
        watching = _Look(False, None, None, None)
    after = [w for w in list_windows() if w.get("focused")]
    was = before[0]["wm_class"] if before else None
    now = after[0]["wm_class"] if after else None
    result["focus"] = ("unchanged: " + str(now)) if was == now else f"moved {was} -> {now}"
    return _look(a, result, watching)


def tool_pointer_scroll(a: dict) -> dict:
    x, y = _point(a)
    dy, dx = int(a.get("dy") or 0), int(a.get("dx") or 0)
    if not dy and not dx:
        raise ToolError("give dy (down is positive) or dx (right is positive)",
                        code="bad_args")
    if max(abs(dy), abs(dx)) > 30:
        raise ToolError("more than 30 wheel clicks at once is almost always a typo",
                        code="bad_args")
    guard = _guard_point(x, y, a["expect_window"]) if a.get("expect_window") else None
    watching = _look_before(a, point=(x, y))
    _pointer().scroll(x, y, dy=dy, dx=dx)
    result = {"action": "scroll", "x": x, "y": y, "dy": dy, "dx": dx, "guard": guard}
    return _look(a, result, watching)


def tool_pointer_position(_: dict) -> dict:
    return pointer_position()


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

    # Every call REPLACES the ref table, even one that finds no widgets: refs
    # describe the screen as it was at their own screen_map call, so the moment
    # a newer call exists they are stale by definition.
    widgets: list[dict] = []
    app = None
    window_id = None
    if a.get("widgets", True):
        app = a.get("app")
        if not app:
            focused = [w for w in windows if w.get("focused")]
            app = _atspi_app_for_window(focused[0]) if focused else None
        if app:
            try:
                widgets = _clickable_widgets(app, int(a.get("limit") or 60))
                win = _window_for_atspi_app(app, windows)
                window_id = win["id"] if win else None
                out["widgets"] = widgets       # register_refs numbers them in place
                out["widgets_app"] = app
            except ToolError as e:
                out["widgets"] = f"unavailable: {e}"
        else:
            out["widgets"] = ("no AT-SPI application matched the focused window; "
                              "pass app= to map a different one")
    out["refs_generation"] = register_refs(widgets, app, window_id)
    if widgets:
        out["refs_note"] = REFS_CONTRACT
    return out


# =========================================================================
# clipboard
# =========================================================================
WL_TIMEOUT_S = 3.0

_WLCOPY_CAVEAT = (
    "set via wl-copy, not from inside gnome-shell: mutter has a measured bug "
    "where an external client's offer can serve the wrong bytes to text "
    "requests (FINDINGS S-018, documented in the extension source), so a paste "
    "into some apps may arrive corrupted. The extension route avoids it; it "
    "needs the extension loaded and the screen unlocked."
)


def _unwrap_bool_string(raw: str) -> tuple[bool, str]:
    """gdbus prints a (b,s) reply as `(true, 'detail')`. The boolean is
    lowercase, which ast.literal_eval refuses, so it is split off first and
    only the string half -- whichever quoting gdbus chose -- is literal_eval'd.
    """
    m = re.fullmatch(r"\((true|false),\s*(.*)\)", raw.strip(), re.DOTALL)
    if not m:
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}",
                        code="extension_unavailable")
    try:
        detail = ast.literal_eval(m.group(2))
    except (SyntaxError, ValueError):
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}",
                        code="extension_unavailable") from None
    return m.group(1) == "true", str(detail)


def _wl(binary: str, *args: str, stdin: bytes | None = None,
        timeout: float = WL_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run wl-copy / wl-paste with a deadline.

    The deadline is not paranoia: wl-paste blocks forever when a client offers
    the clipboard but never serves the request, and a tool that can hang is
    worse than one that reports it could not read.
    """
    if not shutil.which(binary):
        raise ToolError(
            f"{binary} is not installed, so the clipboard cannot be reached "
            "this way. It is in the wl-clipboard package.",
            code="input_backend_failed",
        )
    try:
        return subprocess.run([binary, *args], input=stdin, check=False,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"{binary} did not finish within {timeout:.0f}s. The clipboard "
            "owner is offering content but not serving it, so the clipboard "
            "is effectively unreadable right now.",
            code="timeout",
        ) from None


def _read_clipboard_text() -> tuple[str | None, str]:
    """The clipboard as text, or (None, why). why == "empty" is the clean case."""
    # The bundled extension reads the clipboard from inside gnome-shell, which
    # keeps working while the screen is locked -- wl-paste does not. Prefer it
    # when the loaded extension has it; fall through on any wobble.
    if "GetClipboardText" in extension_methods():
        try:
            reply = _gdbus("GetClipboardText", timeout=5.0)
            parsed = ast.literal_eval(
                reply.replace("true", "True").replace("false", "False"))
            if isinstance(parsed, tuple) and len(parsed) == 2:
                ok, text = parsed
                if ok:
                    return str(text), ""
        except (ToolError, SyntaxError, ValueError):
            pass
    proc = _wl("wl-paste", "--no-newline")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace").strip()
        low = err.lower()
        if "nothing is copied" in low or not err:
            return None, "empty"
        if "no suitable type" in low:
            return None, ("the clipboard holds no text, only non-text types; "
                          "clipboard_read{types: true} lists them")
        return None, err[:200]
    try:
        return proc.stdout.decode(), ""
    except UnicodeDecodeError:
        return None, (f"the clipboard offer is not valid UTF-8 "
                      f"({len(proc.stdout)} bytes); read the types instead")


def tool_clipboard_read(a: dict) -> dict:
    if a.get("types"):
        proc = _wl("wl-paste", "--list-types")
        if proc.returncode != 0:
            return {"types": [], "empty": True,
                    "detail": "the clipboard is empty -- a state, not a failure"}
        types = [t.strip() for t in proc.stdout.decode(errors="replace").splitlines()
                 if t.strip()]
        return {"types": types, "count": len(types)}

    text, why = _read_clipboard_text()
    if text is not None:
        return {"text": text, "characters": len(text)}
    if why == "empty":
        return {"text": None, "empty": True,
                "detail": "the clipboard is empty; nothing was there to read"}
    return {"text": None, "detail": why}


def tool_clipboard_write(a: dict) -> dict:
    text, path, mimetype = a.get("text"), a.get("path"), a.get("mimetype")
    if (text is None) == (path is None):
        raise ToolError(
            "give exactly one of text (a string to place on the clipboard) or "
            "path + mimetype (a file whose bytes go there)",
            code="bad_args",
        )
    if text is not None:
        if not isinstance(text, str) or text == "":
            raise ToolError("text must be a non-empty string", code="bad_args")
        return _clipboard_write_text(text)
    if not isinstance(mimetype, str) or "/" not in mimetype:
        raise ToolError("path needs a mimetype, e.g. image/png", code="bad_args")
    file = Path(os.path.expanduser(str(path))).absolute()
    if not file.is_file():
        raise ToolError(f"{file} does not exist or is not a file", code="bad_args")
    return _clipboard_write_file(file, mimetype)


def _gvariant_string_literal(text: str) -> str:
    """`text` as a GVariant text-format string literal.

    gdbus parses each positional argument with g_variant_parse, and when that
    fails it retries with the argument wrapped in quotes -- escaping the quotes
    but NOT the backslashes (_g_variant_parse_me_harder in gdbus-tool.c). So a
    payload containing '\\b' arrived as byte 0x08, measured here 2026-08-23,
    and a payload that IS a quoted string would silently lose its quotes.
    Serialising the literal ourselves makes the first parse succeed and decode
    exactly what was encoded, so neither heuristic ever runs.
    """
    out = ['"']
    for ch in text:
        if ch in ('"', '\\'):
            out.append("\\" + ch)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _set_via_extension(result: dict, method: str, *args: str) -> bool:
    """Try the extension route; record why it did not happen rather than dying.

    Setting the clipboard from inside gnome-shell is the whole point of the
    method existing (S-018: external offers can be served wrong), so failure
    here downgrades to wl-copy instead of failing the write.
    """
    if method not in extension_methods():
        result["extension_error"] = _needs_relogin(method)
        return False
    try:
        # "--" stops gdbus option parsing, so a payload starting with "-"
        # cannot be eaten as an option (verified 2026-08-23: GOption strips
        # the "--"), and each argument goes over as a self-serialised GVariant
        # literal so gdbus's lossy re-quoting fallback never triggers.
        ok, detail = _unwrap_bool_string(
            _gdbus(method, "--", *(_gvariant_string_literal(v) for v in args)))
    except ToolError as e:
        result["extension_error"] = str(e)[:200]
        return False
    if not ok:
        result["extension_error"] = detail[:200]
        return False
    return True


def _clipboard_write_text(text: str) -> dict:
    result: dict[str, Any] = {"characters": len(text)}
    if _set_via_extension(result, "SetClipboardText", text):
        result["via"] = "extension (gnome-shell set the clipboard itself)"
    else:
        proc = _wl("wl-copy", stdin=text.encode(), timeout=10.0)
        if proc.returncode != 0:
            raise ToolError(
                "wl-copy failed: "
                f"{(proc.stderr or b'').decode(errors='replace').strip()[:200]}",
                code="input_backend_failed",
            )
        result["via"] = "wl-copy fallback"
        result["caveat"] = _WLCOPY_CAVEAT

    # Verify what LANDED, not what was sent -- same rule as type_text.
    got: str | None = None
    why = ""
    try:
        for attempt in range(2):
            got, why = _read_clipboard_text()
            if got == text:
                result["verified"] = True
                result["detail"] = (f"wrote {len(text)} characters and read "
                                    "them back identical")
                return result
            time.sleep(0.2)
    except ToolError as e:
        why = str(e)
    result["verified"] = False
    result["verify_detail"] = (
        f"read back {got[:120]!r} instead" if got is not None
        else f"could not read back: {why}")
    return result


def _clipboard_write_file(file: Path, mimetype: str) -> dict:
    payload = file.read_bytes()
    if not payload:
        raise ToolError(f"{file} is empty; refusing to clear the clipboard "
                        "with an empty offer", code="bad_args")
    result: dict[str, Any] = {"file": str(file), "mimetype": mimetype,
                              "bytes": len(payload)}
    if _set_via_extension(result, "SetClipboardFile", mimetype, str(file)):
        result["via"] = ("extension (gnome-shell read the file and set the "
                         "clipboard itself)")
    else:
        proc = _wl("wl-copy", "--type", mimetype, stdin=payload, timeout=15.0)
        if proc.returncode != 0:
            raise ToolError(
                "wl-copy failed: "
                f"{(proc.stderr or b'').decode(errors='replace').strip()[:200]}",
                code="input_backend_failed",
            )
        result["via"] = "wl-copy fallback"
        result["caveat"] = _WLCOPY_CAVEAT

    try:
        back = _wl("wl-paste", "--type", mimetype, timeout=10.0)
        if back.returncode == 0 and back.stdout == payload:
            result["verified"] = True
            result["detail"] = f"read all {len(payload)} bytes back identical"
        else:
            types = _wl("wl-paste", "--list-types")
            offered = [t.strip() for t in
                       types.stdout.decode(errors="replace").splitlines()
                       if t.strip()]
            result["verified"] = False
            result["offered_types"] = offered[:8]
            result["verify_detail"] = (
                f"reading the {mimetype} offer back returned "
                f"{len(back.stdout)} bytes for a {len(payload)}-byte file"
                if back.returncode == 0 else
                f"the clipboard does not serve {mimetype}")
    except ToolError as e:
        result["verified"] = False
        result["verify_detail"] = f"could not read back: {e}"
    return result
