from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Callable

from .atspi import (
    DEFAULT_FIND_DEPTH,
    DEFAULT_TREE_DEPTH,
    list_atspi_apps,
    tool_launch_app,
    tool_ui_apps,
    tool_ui_find,
    tool_ui_press,
    tool_ui_read_text,
    tool_ui_set_text,
    tool_ui_tree,
)
from .capture import (
    _INLINE_KEY,
    SETTLE_MAX_S,
    tool_frames,
    tool_region_changed,
    tool_screencast,
    tool_screenshot,
    tool_zoom,
)
from .config import KEYS, MODIFIERS
from .errors import ToolError
from .input import (
    YDOTOOL_SOCKET,
    _input,
    _pointer,
    keyboard_layouts,
    layout_hazard,
    parse_combo,
    pointer_position,
    tool_clipboard_read,
    tool_clipboard_write,
    tool_hold_key,
    tool_pointer_click,
    tool_pointer_drag,
    tool_pointer_move,
    tool_pointer_position,
    tool_pointer_scroll,
    tool_press_keys,
    tool_screen_map,
    tool_type_text,
)
from .journal import record as journal_record
from .journal import tool_journal
from .ocr import OCR_MIN_CONFIDENCE, tool_find_text
from .shell import (
    _extension_diagnosis,
    _extension_state,
    _needs_relogin,
    extension_methods,
    list_windows,
    halt_active,
    tool_activate_window,
    tool_assert_state,
    tool_list_windows,
    tool_wait_for,
    tool_window_at,
    tool_window_manage,
    window_at,
)
from .steps import DO_STEPS_MAX, tool_do_steps

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "wayland-computer-use", "version": "1.0.0"}


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
    "description": 'Rectangle for look:"region", in screen pixels. Object form '
                   '{x, y, width, height} or array form [x, y, width, height].',
    # _parse_region has always accepted both forms; the schema said object-only,
    # so a strictly-validating host rejected the array form the code supports.
    "anyOf": [
        {"type": "object",
         "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"}}},
        {"type": "array", "items": {"type": "integer"},
         "minItems": 4, "maxItems": 4},
    ],
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
        },
        "handler": tool_screenshot,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "zoom",
        "description": "Look closer at a small area at FULL resolution -- never "
                       "scaled, unlike screenshot, which fits everything to the "
                       "model's 1568px ceiling. For a tiny glyph, a hairline "
                       "border, an icon. Refuses more than half the desktop: "
                       "zoom exists to spend tokens on FEW pixels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"description": "Rectangle in screen pixels, [x, y, "
                                          "width, height] or {x, y, width, "
                                          "height}. With window=, measured "
                                          "inside that window.",
                           "anyOf": [{"type": "array"}, {"type": "object"}]},
                "window": {"description": "Zoom into this window (id, wm_class "
                                          "or title fragment).",
                           "anyOf": [{"type": "integer"}, {"type": "string"}]},
                "pad": {"type": "integer", "default": 0, "minimum": 0,
                        "description": "Extra pixels of context on every side."},
                "path": {"type": "string",
                         "description": "Where to keep the PNG; defaults to "
                                        "the shot cache"},
            },
        },
        "handler": tool_zoom,
        "annotations": {"readOnlyHint": True},
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
                "ref": {"type": "integer",
                        "description": "A widget number from the last screen_map "
                                       "-- the click lands at that widget's "
                                       "CURRENT position after its identity is "
                                       "re-verified, no coordinates needed. Give "
                                       "ref OR x/y, never both. Refs die at the "
                                       "next screen_map call."},
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
            "required": [],
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
                       "button' into a number without looking at an image. Every "
                       "widget also carries a ref: N -- pass it straight to "
                       "ui_press(ref) or pointer_click(ref); refs are valid until "
                       "the next screen_map call, and the result's refs_generation "
                       "says which call issued them.",
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
                       "window_gone, window_focused, focus_changes; text_appears "
                       "(OCR polls a window for a string -- a reply arriving, a "
                       "build finishing); widget_exists (an AT-SPI widget matching "
                       "text/role shows up in app); clipboard_changed (a copy "
                       "landed). Returns as soon as it is true, or reports honestly "
                       "that it timed out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string",
                              "enum": ["window_exists", "window_gone",
                                       "window_focused", "focus_changes",
                                       "text_appears", "widget_exists",
                                       "clipboard_changed"]},
                "target": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "text": _s("For text_appears: the string to watch for. For "
                           "widget_exists: the widget name to match."),
                "app": _s("For widget_exists: the AT-SPI application name"),
                "role": _s("For widget_exists: the widget role, e.g. 'push "
                           "button'"),
                "timeout": {"type": "number", "default": 10, "minimum": 0.2,
                            "maximum": 120},
            },
            "required": ["condition"],
        },
        "handler": tool_wait_for,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "assert_state",
        "description": "Prove the desktop is in a state, with evidence -- the "
                       "honest way to END a task. Each assertion comes back "
                       "passed/failed with what was actually observed; a false "
                       "assertion is a result, not an error. Give any of: "
                       "window_exists, window_focused, text_present, "
                       "widget_exists, clipboard_contains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_exists": {"anyOf": [{"type": "integer"},
                                            {"type": "string"}],
                                  "description": "A window id, wm_class or "
                                                 "title fragment that must "
                                                 "exist."},
                "window_focused": {"anyOf": [{"type": "integer"},
                                             {"type": "string"}],
                                   "description": "A window that must exist "
                                                  "AND hold focus."},
                "text_present": {"type": "object",
                                 "description": '{"text": ..., "window": ...}: '
                                                "the string must be visible "
                                                "(OCR) in that window."},
                "widget_exists": {"type": "object",
                                  "description": '{"app": ..., "text" and/or '
                                                 '"role"}: a matching AT-SPI '
                                                 "widget must exist."},
                "clipboard_contains": _s("The clipboard text must contain "
                                         "this string."),
            },
        },
        "handler": tool_assert_state,
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
                       "action depends on what the last one revealed. The sequence is "
                       "validated up front -- a call that cannot finish never starts.",
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
                        "set_text(path,text), wait_for(condition,target,timeout) -- "
                        "same arguments as the wait_for tool; 'wait' is an alias -- "
                        "and sleep(ms, 1-60000; prefer a wait_for condition over a "
                        "guessed duration). A malformed step anywhere refuses the "
                        "whole call naming that step, and nothing executes. Any "
                        'step may carry retry: {"attempts": N, "on": [codes]} -- '
                        "it reruns on those error codes (default: the "
                        "world-moved set) and reports how many runs it took."),
                    "items": {"type": "object"},
                },
                "look_at": _LOOK_AT_SCHEMA,
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
        "annotations": {"readOnlyHint": True},
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
        "name": "window_manage",
        "description": "Move, resize, close, (un)minimize, (un)maximize, "
                       "re-workspace or pin a window -- through the "
                       "compositor, where these are ordinary calls. The "
                       "result reports the window as it IS afterwards (new "
                       "geometry, or gone), not just that the call was sent. "
                       "close that leaves the window standing names the "
                       "usual reason: an unsaved-changes dialog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["move_resize", "close", "minimize",
                                    "unminimize", "maximize", "unmaximize",
                                    "workspace", "above"]},
                "target": {"anyOf": [{"type": "integer"}, {"type": "string"}],
                           "description": "Window id, wm_class or title "
                                          "fragment."},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "index": {"type": "integer",
                          "description": "Workspace index, for action: "
                                         "workspace"},
                "above": {"type": "boolean", "default": True,
                          "description": "For action: above -- pin or unpin."},
            },
            "required": ["action", "target"],
        },
        "handler": tool_window_manage,
    },
    {
        "name": "launch_app",
        "description": "Start an application and confirm it actually arrived: "
                       "the result carries the NEW window's dict (or, while the "
                       "screen is locked, the new AT-SPI app) and names which "
                       "mechanism confirmed. Every real task starts with an app "
                       "that is not running yet; this is that step, inside the "
                       "protocol instead of a shell command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "desktop_id": _s("Desktop id for `gio launch`, with or without "
                                 "'.desktop', e.g. 'org.gnome.TextEditor'"),
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "Argv list, not a shell string. "
                                           "Exactly one of desktop_id/command."},
                "file": _s("Optional file for the app to open"),
                "wait_window": {"type": "boolean", "default": True,
                                "description": "Confirm arrival by a NEW window "
                                               "id (or a new AT-SPI app when "
                                               "the extension is down)."},
                "timeout": {"type": "number", "default": 15, "minimum": 0.5,
                            "maximum": 120},
            },
        },
        "handler": tool_launch_app,
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
                "path": _s("Or an exact index path. Both tools return the resolved path -- pass it back to address the SAME document across write and read; without it, both prefer the focused text widget."),
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
                "path": _s("Or an exact index path. Both tools return the resolved path -- pass it back to address the SAME document across write and read; without it, both prefer the focused text widget."),
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
                "ref": {"type": "integer",
                        "description": "A widget number from the last "
                                       "screen_map; path, expect_name and "
                                       "expect_role are filled from it. Give "
                                       "ref OR path, never both."},
                "expect_name": _s("Name the widget should still have (substring)"),
                "expect_role": _s("Role the widget should still have"),
                "action_index": {"type": "integer", "default": 0},
            },
            "required": [],
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
        "name": "clipboard_write",
        "description": "Put text or a file's bytes on the clipboard, and PROVE it "
                       "landed by reading it back. Goes through the gnome-shell "
                       "extension so the compositor sets the clipboard itself -- "
                       "mutter has a measured bug (S-018) where an external "
                       "client's offer can serve wrong bytes to text requests, so "
                       "wl-copy is only the fallback and says so when used.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _s("Text to place on the clipboard. Give this OR "
                           "path+mimetype, not both."),
                "path": _s("File whose bytes go on the clipboard, e.g. a PNG"),
                "mimetype": _s("The type those bytes are offered as, "
                               "e.g. image/png. Required with path."),
            },
        },
        "handler": tool_clipboard_write,
    },
    {
        "name": "clipboard_read",
        "description": "What is on the clipboard. Text by default; types:true "
                       "lists the offered mimetypes instead. An empty clipboard "
                       "is a clean result, not an error, and a clipboard owner "
                       "that never serves its offer is reported after a short "
                       "deadline instead of hanging.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "types": {"type": "boolean", "default": False,
                          "description": "List offered mimetypes instead of "
                                         "reading text."},
            },
        },
        "handler": tool_clipboard_read,
        "annotations": {"readOnlyHint": True},
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
        "name": "hold_key",
        "description": "Hold ONE key down for a duration, then release it -- a "
                       "real press and a separate release, not a tap. For "
                       "shift-selection, held-key scrolling and games. Blocks "
                       "for the whole duration; press_keys is the tool for "
                       "combinations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "look": _LOOK_SCHEMA,
                "look_at": _LOOK_AT_SCHEMA,
                "settle_max_s": _SETTLE_SCHEMA,
                "key": _s("One key, e.g. 'shift', 'down', 'w'. No combos."),
                "seconds": {"type": "number", "minimum": 0.05, "maximum": 300,
                            "description": "How long to hold. The call blocks "
                                           "for this long."},
                "target": TARGET_SCHEMA,
            },
            "required": ["key", "seconds"],
        },
        "handler": tool_hold_key,
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
    {
        "name": "journal",
        "description": "Read back the trail of acted tool calls -- every "
                       "state-changing call is journaled with its arguments, "
                       "outcome, hit/miss verdict and screenshot hash. Use it to "
                       "reconstruct what already happened after context loss, or "
                       "to review an unattended run. Reading tools are not in it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tail": {"type": "integer", "default": 20, "minimum": 1,
                         "maximum": 1000,
                         "description": "How many of the most recent entries to "
                                        "return, oldest first."},
                "session": {"type": "string", "enum": ["current", "all"],
                            "default": "current",
                            "description": "current: only this server process's "
                                           "actions. all: every session in the "
                                           "14-day retention window."},
            },
        },
        "handler": tool_journal,
        "annotations": {"readOnlyHint": True},
    },
]

HANDLERS: dict[str, Callable[[dict], Any]] = {t["name"]: t["handler"] for t in TOOLS}
_READ_ONLY_TOOLS = {t["name"] for t in TOOLS
                    if (t.get("annotations") or {}).get("readOnlyHint")}
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
        # MCP's ImageContent is {type, data, mimeType} -- flat, and mimeType is
        # camelCase. It is NOT the Anthropic Messages API's nested
        # {type, source:{type, media_type, data}}; that is the shape the HOST
        # converts this into afterwards, which is what a transcript shows and
        # what this was first written against. The host rejects the wrong one
        # outright with a union-validation error naming every content type.
        blocks.append({"type": "image",
                       "data": image["data"],
                       "mimeType": image["media_type"]})
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
        args = params.get("arguments") or {}
        acted = name not in _READ_ONLY_TOOLS
        try:
            # Deferred headless start (see mcp_server._resolve_session): the
            # session was cold at initialize, so the FIRST tool call pays the
            # 15-20s bring-up here, inside a normal per-call timeout, instead
            # of inside the MCP client's connect window. The marker is cleared
            # only on success, so a failed start is retried by the next call
            # (its ToolError still reaches the model either way).
            if os.environ.get("WCU_HEADLESS_LAZY"):
                from .headless import ensure, pin_env, session_name
                _, _, _pinned = os.environ.get("WCU_SESSION", "").partition(":")
                pin_env(ensure(name=session_name(_pinned or None)))
                os.environ.pop("WCU_HEADLESS_LAZY", None)
            # The kill switch gates every state-changing tool at one choke
            # point. Reading tools keep working while halted -- a human who
            # stopped the hands still wants the eyes.
            if acted and halt_active():
                raise ToolError(
                    "the human halt switch is engaged (Super+Ctrl+Escape). "
                    "Nothing will be injected until a human presses it again "
                    "or calls ClearHalt. Reading tools still work.",
                    code="halted",
                )
            result = handler(args)
            # Evidence, not surveillance: every acted call leaves a trail a
            # human can review after an unattended run and an agent can
            # re-read after context loss. record() never raises.
            if acted:
                journal_record(name, args, result)
            _respond(msg_id, {"content": _content_blocks(result)})
        except ToolError as e:
            # A tool-level failure is a result the model must see and reason
            # about, not a protocol error that hides the reason. The [code]
            # prefix is the machine-readable half (see wcu/errors.py).
            if acted:
                journal_record(name, args, {"error": str(e), "code": e.code})
            _respond(msg_id, {"content": [{"type": "text", "text": e.wire_text()}],
                              "isError": True})
        except Exception as e:
            if acted:
                journal_record(name, args,
                               {"error": f"{type(e).__name__}: {e}",
                                "code": "crash"})
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
