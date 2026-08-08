#!/usr/bin/env python3
"""MCP server for driving this GNOME/Wayland desktop.

The point of this file is that "hey Claude, do this on my laptop" should work in
every session without ceremony -- no remembering a script path, no shell quoting,
no separate setup step.

WHAT IT IS FOR AND WHAT IT REFUSES

Three mechanisms, because Wayland hands a client none of them directly:

  * gnome-shell extension over D-Bus (`org.tristan.MigrationHelpers`) for
    screenshots, window geometry, and focus. gnome-shell refuses
    `org.gnome.Shell.Screenshot` and `GrabAccelerator` to ordinary clients, and
    `grim` is wlroots-only, so an extension running inside the shell is the only
    path that exists on GNOME.
  * AT-SPI for anything semantic. `ui_find` then `ui_press` presses the real
    widget, so it cannot miss, cannot be defeated by a window moving, and needs
    no pointer at all. **Prefer this over typing and key combos.**
  * ydotool through /dev/uinput for text and key combos. This is the weak one:
    injection is focus-blind, it types wherever focus happens to be.

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
    a session that guesses wrong burns an hour. INACTIVE-while-enabled almost
    always means the screen is locked: gnome-shell unloads every extension whose
    metadata.json does not list "unlock-dialog" in session-modes, and this one
    lists only "user".
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
    """gdbus prints ('<payload>',) with the payload escaped."""
    match = re.match(r"^\('(.*)',\)$", raw, re.S)
    if not match:
        raise ToolError(f"unexpected D-Bus reply shape: {raw[:200]}")
    return match.group(1).encode().decode("unicode_escape")


def list_windows() -> list[dict]:
    return json.loads(_unwrap_gvariant_string(_gdbus("ListWindows")))


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
            f"no window with id {wanted}. Open windows: "
            + ", ".join(f'{w["id"]} ({w["wm_class"]})' for w in windows)
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
    for node in candidates:
        live = _resolve_path(node["path"])
        _, editable = _text_ifaces(live)
        if editable is not None:
            return live
    return _resolve_path(candidates[0]["path"])


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


def tool_screenshot(a: dict) -> dict:
    path = Path(os.path.expanduser(str(a.get("path") or ""))).absolute()
    if path.is_dir():
        raise ToolError(f"{path} is a directory; give a file path ending in .png")
    # The suffix check is load-bearing, not cosmetic: this used to unlink whatever
    # already existed at the caller-supplied path before capturing, so
    # screenshot{"path": "~/system/healthcheck.sh"} deleted that file -- and if the
    # capture then failed (a locked screen is enough), it was simply gone.
    if path.suffix.lower() != ".png":
        raise ToolError(f"path must end in .png, got {path.name!r}")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Capture to a temp file beside the target and rename on success, so an
    # existing file is only ever replaced by a real screenshot.
    tmp = path.with_name(f".{path.name}.capturing")
    tmp.unlink(missing_ok=True)
    try:
        _gdbus("Screenshot", str(tmp), "true" if a.get("include_cursor") else "false")
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ToolError("the call returned but no image was written")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    with path.open("rb") as fh:
        if fh.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ToolError(f"{path} was written but is not a PNG")
    dims = ""
    if shutil.which("file"):
        out = subprocess.run(["file", "-b", str(path)], capture_output=True,
                             text=True, timeout=15).stdout
        m = re.search(r"(\d+) x (\d+)", out)
        if m:
            dims = f"{m.group(1)}x{m.group(2)}"
    return {"path": str(path), "bytes": path.stat().st_size, "dimensions": dims}


def tool_activate_window(a: dict) -> dict:
    return focus_window(a.get("target"))


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
    if not text:
        raise ToolError("text is required")
    role = a.get("role")
    depth = int(a.get("depth") or DEFAULT_FIND_DEPTH)
    actionable_only = bool(a.get("actionable_only", False))

    roots = [_find_app(str(a["app"]))] if a.get("app") else None
    if roots is None:
        Atspi = _atspi()
        desk = Atspi.get_desktop(0)
        roots = [desk.get_child_at_index(i) for i in range(desk.get_child_count())]
        roots = [r for r in roots if r is not None]

    needle = text.lower()
    hits: list[dict] = []
    for root in roots:
        collected: list[dict] = []
        _walk(root, root.get_name(), 0, depth, collected, cap=MAX_FIND_NODES)
        for node in collected:
            if needle not in node["name"].lower() and needle not in node["path"].lower():
                continue
            if role and node["role"] != role:
                continue
            if actionable_only and not node.get("actions"):
                continue
            hits.append(node)
    return {
        "query": text, "matches": len(hits), "results": hits[:40],
        "hint": ("pass the path plus expect_name or expect_role to ui_press; "
                 "pressing the real widget cannot miss, and paths go stale as soon "
                 "as the tree changes"),
    }


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
    ok = node.do_action(index)
    if not ok:
        raise ToolError(f"do_action({index}) on {path} returned false; nothing happened")
    return {"path": path, "widget": actual_name, "role": actual_role,
            "action": action_name,
            "detail": f"pressed {action_name!r} on {actual_name!r} [{actual_role}]"}


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

    # Read the widget BEFORE typing so we can tell what this call actually added.
    app_hint = a.get("verify_app") or _atspi_app_for_window(focus["window"])
    before = None
    if app_hint:
        try:
            before = _read_text(_find_text_widget(str(app_hint), None))
        except ToolError:
            before = None

    _ydotool("type", "--key-delay", str(delay), text,
             timeout=max(30.0, len(text) * delay / 1000 + 15))

    result = {"characters": len(text), "focus": focus["detail"],
              "detail": f'sent {len(text)} characters to {focus["window"]["wm_class"]}'}
    hazard = layout_hazard()
    if hazard:
        result["layout_warning"] = hazard

    # Verify what LANDED, not what was sent. ydotool exits 0 whether or not the
    # right characters arrived, and on a non-US layout they demonstrably do not.
    if before is None:
        result["verified"] = False
        result["detail"] += (" -- COULD NOT VERIFY: no readable AT-SPI text widget, "
                            "so there is no proof these characters arrived intact"
                            + (f". {hazard}" if hazard else ""))
        return result

    time.sleep(0.4)
    try:
        after = _read_text(_find_text_widget(str(app_hint), None))
    except ToolError:
        result["verified"] = False
        result["detail"] += " -- could not re-read the widget to verify"
        return result

    added = after[len(before):] if after.startswith(before) else after
    if text in added or text in after[len(before):]:
        result["verified"] = True
        result["detail"] = (f'typed {len(text)} characters into '
                            f'{focus["window"]["wm_class"]} and read them back')
        return result

    raise ToolError(
        f"ydotool reported success but the wrong characters arrived. Sent {text!r}, "
        f"the widget gained {added!r}. Nothing here is retryable -- this is the "
        f"keycode/layout mismatch, not a race. {hazard or ''} "
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
    sequence = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    _ydotool("key", "--key-delay", "40", *sequence)
    return {"combo": combo, "focus": focus["detail"],
            "detail": f'sent {combo} to {focus["window"]["wm_class"]}'}


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

    report["ydotool"] = (
        "ready" if shutil.which("ydotool") and os.path.exists(YDOTOOL_SOCKET)
        else "unavailable (ydotoold socket missing or ydotool not installed)"
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

TOOLS: list[dict] = [
    {
        "name": "list_windows",
        "description": "Every open window with id, wm_class, title, geometry, and "
                       "which one has focus. Start here: ids from this list are what "
                       "type_text and press_keys target.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_windows,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "screenshot",
        "description": "Capture the whole screen to a PNG and verify it really is a "
                       "PNG with real dimensions. Goes through the gnome-shell "
                       "extension because the compositor refuses screenshots to "
                       "ordinary clients and grim never works on GNOME.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _s("Where to write the PNG, e.g. /tmp/shot.png"),
                "include_cursor": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "handler": tool_screenshot,
    },
    {
        "name": "activate_window",
        "description": "Focus and raise a window, then confirm focus actually landed "
                       "there. Returns an error rather than a false success.",
        "inputSchema": {"type": "object", "properties": {"target": TARGET_SCHEMA},
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
                "text": _s("Substring to look for in widget names"),
                "app": _s("Restrict to one application (much faster)"),
                "role": _s("Require an exact AT-SPI role, e.g. push_button"),
                "actionable_only": {"type": "boolean", "default": False,
                                    "description": "Only widgets that expose an action"},
                "depth": {"type": "integer", "default": DEFAULT_FIND_DEPTH,
                          "description": "GTK4 nests deeply -- the default is 30 "
                                         "because a text view can sit at depth 23"},
            },
            "required": ["text"],
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
        "description": "Last-resort text entry: injects keystrokes with ydotool into a "
                       "named window. Focus is confirmed first, nothing is typed if it "
                       "cannot be confirmed, and the widget is read back afterwards to "
                       "check the right characters arrived. TRY ui_set_text FIRST. On "
                       "this machine the keyboard layout is German (QWERTZ) and ydotool "
                       "injects US-QWERTY keycodes, so typed y/z and most punctuation "
                       "arrive TRANSPOSED -- measured, not theoretical. ui_set_text "
                       "passes characters to the widget and is immune.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _s("Literal text to type"),
                "target": TARGET_SCHEMA,
                "key_delay_ms": {"type": "integer", "default": 20},
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
        "description": "Send a key combination to a named window, e.g. ctrl+s. Focus "
                       "is confirmed first. Ctrl+Alt+F1-F12 is refused: it switches "
                       "virtual terminal and looks exactly like a frozen machine.",
        "inputSchema": {
            "type": "object",
            "properties": {"combo": _s("e.g. 'ctrl+shift+t'"), "target": TARGET_SCHEMA},
            "required": ["combo", "target"],
        },
        "handler": tool_press_keys,
    },
    {
        "name": "desktop_health",
        "description": "Whether each mechanism is usable right now: extension state, "
                       "window count, AT-SPI app count, ydotool socket, "
                       "toolkit-accessibility, session type. Call this first when "
                       "something behaves oddly.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_health,
        "annotations": {"readOnlyHint": True},
    },
]

HANDLERS: dict[str, Callable[[dict], Any]] = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SCHEMAS = [
    {k: v for k, v in t.items() if k != "handler"} for t in TOOLS
]


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
            _respond(msg_id, {"content": [{"type": "text",
                                           "text": json.dumps(result, indent=1)}]})
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
        tool_screenshot({"path": "/tmp/wcu-selftest.png"})))
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

    failed = 0
    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label:<24} {detail[:150]}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


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
