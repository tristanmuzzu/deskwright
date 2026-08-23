from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from .atspi import (
    _atspi_app_for_window,
    _clickable_widgets,
    _find_text_widget,
    _read_text,
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
