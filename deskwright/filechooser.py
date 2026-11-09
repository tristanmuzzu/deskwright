"""choose_file: fill an open file chooser with a path, or say that none is open.

GTK file choosers were called "an agent tarpit" on the roadmap in August
(item 18) and stayed one: on 2026-11-09 an agent clicked a browser's upload
button, could not tell whether a chooser had opened (none was listed and
none was visible), and fell back to splitting a file into 10 MB parts and
rejoining it in page JavaScript -- about ten extra calls.

What this does, in order:
  1. find the chooser: the window named in `target`, else a window owned by
     xdg-desktop-portal-gnome/-gtk (the portal draws choosers for sandboxed
     and portal-using apps), else Nautilus's "Pick Files" window (GNOME 50's
     portal delegates to it), else a dialog whose title reads like a chooser;
  2. if none is open, say so plainly, with the dialog-like windows that ARE
     open, so "suppressed" and "unlisted" can be told apart;
  3. focus it, open the location field (ctrl+L, which GTK3 and GTK4 both
     honour), type the path through the guarded type_text, press Return;
     if a chooser is still open a second later on a file path (Nautilus
     selects the file and waits), press its one Open/Select/Save button;
  4. report whether the chooser closed (accepted), or what is still open --
     an overwrite confirmation is reported, never answered.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from .errors import ToolError

PORTAL_PROCESSES = ("xdg-desktop-portal-gnome", "xdg-desktop-portal-gtk",
                    "xdg-desktop-portal-kde")
# On GNOME 50 the portal hands the chooser to Nautilus: a NORMAL, non-transient
# window of `nautilus --gapplication-service` titled "Pick Files" (seen live on
# 2026-11-09 for gnome-text-editor's ctrl+O). An ordinary Nautilus window is
# titled after its folder, so for Nautilus the title must START with a chooser
# verb; a folder named "Open" alone would still need `target`.
CHOOSER_HOSTS = ("nautilus",)
_HOST_TITLE = re.compile(r"^(pick|open|save|select|choose|upload|export|import|attach)\b"
                         r".*\b(file|files|folder|folders|location)\b", re.IGNORECASE)
_TITLE = re.compile(
    r"\b(open|save|select|choose|upload|export|import|attach|browse|"
    r"öffnen|speichern|auswählen|hochladen|exportieren|ouvrir|enregistrer|"
    r"abrir|guardar)\b", re.IGNORECASE)
_DIALOG_TYPES = {"DIALOG", "MODAL_DIALOG", "NORMAL"}


def _program(pid: Any) -> str:
    """argv[0]'s basename. Not /proc/comm, which cuts names at 15 characters
    ("xdg-desktop-por") and would never match the portal's."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0", 1)[0].decode(errors="replace")
        return os.path.basename(argv0)
    except (OSError, ValueError, TypeError):
        return ""


def find_chooser(windows: list[dict]) -> tuple[dict | None, str]:
    """The open file chooser and how it was recognised, or (None, why not)."""
    portal = [w for w in windows if _program(w.get("pid")) in PORTAL_PROCESSES]
    if portal:
        return portal[-1], "owned by the xdg desktop portal"
    hosted = [w for w in windows if _program(w.get("pid")) in CHOOSER_HOSTS
              and _HOST_TITLE.search(w.get("title") or "")]
    if hosted:
        return hosted[-1], f"the portal's Nautilus chooser {hosted[-1].get('title')!r}"
    titled = [w for w in windows
              if str(w.get("type") or "NORMAL") in _DIALOG_TYPES
              and (w.get("transient_for") or w.get("modal")
                   or str(w.get("type")) != "NORMAL")
              and _TITLE.search(w.get("title") or "")]
    if titled:
        return titled[-1], f"a dialog titled {titled[-1].get('title')!r}"
    return None, "no window looks like a file chooser"


# Names of a chooser's own confirm button. Pressed only when it is the single
# visible, enabled button with one of these names in the chooser's frame.
_CONFIRM_NAMES = {"open", "select", "save", "upload", "choose", "pick", "insert",
                  "attach", "öffnen", "auswählen", "speichern", "ouvrir", "abrir"}


def _press_confirm(chooser: dict, windows: list[dict]) -> str | None:
    """Press the chooser's confirm button; its name, or None if not exactly one.

    Nautilus (GNOME 50's portal chooser) answers Return in its location bar
    by going to the folder and selecting the file, then waits for Open
    (seen live 2026-11-09). GTK's own chooser accepts on Return, so this only
    runs when the chooser is still open afterwards.
    """
    from . import atspi, semantic
    try:
        Atspi = atspi._atspi()
        app = atspi._find_app(f"#{chooser['pid']}")
        app.clear_cache_single()
        roots = [app.get_child_at_index(i) for i in range(min(app.get_child_count(), 256))]
        root, _ = semantic._map_root([r for r in roots if r is not None], chooser, windows)
        if root is None:
            return None
        found, stack, visited = [], [(root, 0)], 0
        while stack and visited < 4000:
            node, depth = stack.pop()
            visited += 1
            if node is None:
                continue
            if node.get_role_name() in ("push button", "button") and \
                    (node.get_name() or "").strip().lower() in _CONFIRM_NAMES:
                states = node.get_state_set()
                if states.contains(Atspi.StateType.SHOWING) and \
                        states.contains(Atspi.StateType.SENSITIVE):
                    found.append(node)
                continue
            if depth < 40:
                stack.extend((node.get_child_at_index(i), depth + 1)
                             for i in range(min(node.get_child_count(), 500)))
        if len(found) != 1 or not found[0].get_action_iface() or not found[0].do_action(0):
            return None
        return found[0].get_name()
    except Exception:
        return None


def tool_choose_file(a: dict) -> dict:
    from .input import tool_press_keys, tool_type_text
    from .shell import _resolve_target, focus_window, list_windows

    path = a.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path is required: the file (or folder) to choose", code="bad_args")
    path = os.path.expanduser(path.strip())
    if not os.path.isabs(path):
        raise ToolError("path must be absolute, e.g. /home/me/report.pdf; a chooser "
                        "resolves relative names against whatever folder it shows",
                        code="bad_args")
    must_exist = a.get("must_exist", True)
    if must_exist and not os.path.exists(path):
        raise ToolError(f"{path} does not exist. For a save dialog pass must_exist:false.",
                        code="bad_args")

    windows = list_windows()
    if a.get("target") is not None:
        chooser, how = _resolve_target(a["target"]), "named by target"
    else:
        chooser, how = find_chooser(windows)
    if chooser is None:
        dialogs = [{"id": w["id"], "title": w.get("title"),
                    "app": w.get("wm_class") or w.get("owner_app"), "type": w.get("type")}
                   for w in windows if str(w.get("type")) != "NORMAL" or w.get("transient_for")]
        return {"chooser_open": False, "action_status": "not_started",
                "detail": ("no file chooser is open, so nothing was typed. If you just "
                           "clicked an upload or open button, the app may not have opened "
                           "one (some browsers suppress it under automation), or it is "
                           "still appearing: wait_for window_new, then call again. If a "
                           "chooser is visible but not recognised, pass its id as target."),
                "dialogs_open": dialogs[:10]}

    focus_window(chooser["id"])
    tool_press_keys({"combo": "ctrl+l", "target": chooser["id"], "look": False})
    time.sleep(0.25)
    # Clear whatever the location field already holds (a suggested name in a
    # save dialog), then type. type_text verifies by readback where it can.
    tool_press_keys({"combo": "ctrl+a", "target": chooser["id"], "look": False})
    typed = tool_type_text({"text": path, "target": chooser["id"], "look": False})
    tool_press_keys({"combo": "Return", "target": chooser["id"], "look": False})

    is_dir = os.path.isdir(path)
    started = time.monotonic()
    deadline = started + float(a.get("timeout") or 5)
    still, confirmed = chooser, None
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = list_windows()
        still = next((w for w in now if w["id"] == chooser["id"]), None)
        if still is None:
            out = {"chooser_open": False, "accepted": True, "action_status": "applied",
                   "path": path, "recognised": how,
                   "typed_verified": typed.get("verified"),
                   "detail": "the chooser closed after the path was entered; check the "
                             "app shows the file before continuing"}
            if confirmed:
                out["confirmed_with"] = confirmed
            return out
        newer = [w for w in now if w.get("transient_for") == chooser["id"]]
        if newer:
            return {"chooser_open": True, "accepted": False, "action_status": "partial",
                    "path": path, "recognised": how,
                    "follow_up_dialog": {"id": newer[-1]["id"],
                                         "title": newer[-1].get("title")},
                    "detail": "a follow-up dialog opened (often 'replace existing "
                              "file?'). It was NOT answered: look at it and decide."}
        if confirmed is None and not is_dir and time.monotonic() - started > 1.0:
            confirmed = _press_confirm(still, now) or ""
    return {"chooser_open": True, "accepted": False, "action_status": "partial",
            "path": path, "recognised": how, "typed_verified": typed.get("verified"),
            "detail": ("the chooser navigated into that folder and is still open; pick a "
                       "file inside it, or press the dialog's confirm button for folder "
                       "selection" if is_dir else
                       "the chooser is still open after Return; take a screenshot of it "
                       "-- it may show an error, or need its confirm button pressed")}
