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

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# The implementation lives in the wcu/ package. This file stays as the
# executable entry point -- the user-scope MCP registration points at this
# exact path -- and re-exports the public surface for existing importers
# (the tests do `import mcp_server as srv`).
from wcu.atspi import (  # noqa: F401
    DEFAULT_FIND_DEPTH,
    DEFAULT_TREE_DEPTH,
    MAX_FIND_NODES,
    MAX_TREE_NODES,
    TEXT_ROLES,
    list_atspi_apps,
    tool_ui_apps,
    tool_ui_find,
    tool_ui_press,
    tool_ui_read_text,
    tool_ui_set_text,
    tool_ui_tree,
)
from wcu.capture import (  # noqa: F401
    CELL_NOISE,
    CHANGE_FLOOR_PCT,
    FINGERPRINT,
    INLINE_MAX_BYTES,
    INLINE_QUALITY,
    MODEL_MAX_EDGE,
    SETTLE_MAX_S,
    SETTLE_MIN_FRAMES,
    SETTLE_POLL_S,
    SHOT_CACHE,
    SHOT_CACHE_KEEP,
    STABLE_CHANGED_PCT,
    STRONG_CELLS_CHANGED,
    STRONG_CELLS_STABLE,
    STRONG_DELTA,
    tool_frames,
    tool_region_changed,
    tool_screencast,
    tool_screenshot,
)
from wcu.config import KEYS, MODIFIERS  # noqa: F401
from wcu.errors import ToolError  # noqa: F401
from wcu.input import (  # noqa: F401
    YDOTOOL_SOCKET,
    combo_keysyms,
    keyboard_layouts,
    layout_hazard,
    parse_combo,
    pointer_position,
    tool_pointer_click,
    tool_pointer_drag,
    tool_pointer_move,
    tool_pointer_position,
    tool_pointer_scroll,
    tool_press_keys,
    tool_screen_map,
    tool_type_text,
)
from wcu.ocr import (  # noqa: F401
    OCR_MIN_CONFIDENCE,
    OCR_PSM_REGION,
    OCR_PSM_SCREEN,
    OCR_UPSCALE_UNDER,
    tool_find_text,
)
from wcu.server import (  # noqa: F401
    HANDLERS,
    PROTOCOL_VERSION,
    SERVER_INFO,
    TARGET_SCHEMA,
    TOOL_SCHEMAS,
    TOOLS,
    handle,
    self_test,
    serve,
    tool_health,
)
from wcu.shell import (  # noqa: F401
    BUS_NAME,
    EXTENSION_UUID,
    FOCUS_POLL_S,
    FOCUS_TIMEOUT_S,
    OBJ_PATH,
    extension_methods,
    focus_window,
    list_windows,
    tool_activate_window,
    tool_list_windows,
    tool_wait_for,
    tool_window_at,
    window_at,
)
from wcu.steps import DO_STEPS_MAX, tool_do_steps  # noqa: F401

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(serve())
