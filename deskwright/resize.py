"""desktop_resize: restart a private headless desktop at another resolution.

The virtual monitor mutter creates for a headless session has exactly one
mode (GetCurrentState lists only 1280x720@60 on 2026-11-09), so there is no
in-place resolution change to make: the session has to be restarted with a
different --virtual-monitor. That kills every app on it, so this refuses
while any window is open. It never touches the user's own monitors.

The worker that ran this exits after replying, and the next worker re-reads
the session's new bus address (see worker.py), because a Gio connection is
bound to the address it was first opened on.
"""
from __future__ import annotations

import os
from typing import Any

from .errors import ToolError

RESTART_WORKER = False
_IGNORED_TYPES = {"DESKTOP", "DOCK"}


def tool_desktop_resize(a: dict) -> dict:
    global RESTART_WORKER
    if not os.environ.get("DESKWRIGHT_HEADLESS"):
        raise ToolError("desktop_resize only changes a private headless desktop; the "
                        "user's own monitors are theirs to configure", code="wrong_session")
    width, height = a.get("width"), a.get("height")
    for label, value, lo, hi in (("width", width, 640, 3840), ("height", height, 480, 2160)):
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ToolError(f"{label} must be an integer from {lo} to {hi}", code="bad_args")
    from . import headless
    from .shell import list_windows
    name = os.environ.get("DESKWRIGHT_HEADLESS_NAME") or headless.session_name(None)
    current = headless.status(name)
    size = f"{width}x{height}"
    if current.get("size") == size:
        return {"size": size, "restarted": False, "detail": "already this size"}
    open_windows = [w for w in list_windows() if str(w.get("type")) not in _IGNORED_TYPES]
    if open_windows:
        raise ToolError(
            "desktop_resize restarts the private desktop, which would close every app "
            "on it. Close these first: " + ", ".join(
                f'{w.get("wm_class") or w.get("label")} (id {w["id"]})' for w in open_windows[:8]),
            code="bad_args")
    headless.stop(name)
    state: dict[str, Any] = headless.start(size=size, name=name, home=current.get("home"))
    headless.pin_env(state)
    os.environ["DESKWRIGHT_HEADLESS_SIZE"] = size
    RESTART_WORKER = True
    return {"size": state.get("size", size), "restarted": True, "session": name,
            "detail": (f"the private desktop {name!r} was restarted at {size}. Window ids, "
                       "observations and desktop_exec variables from before are gone. "
                       "To start at this size next time, set DESKWRIGHT_HEADLESS_SIZE.")}
