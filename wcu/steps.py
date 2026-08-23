from __future__ import annotations

import json
import time
from typing import Any

from .atspi import _window_for_atspi_app
from .capture import _Look, _look, _look_before
from .errors import ToolError
from .shell import _resolve_target, window_at


def _step_window_hint(step: dict) -> dict | None:
    """The window a step will act on, from whichever way it names one."""
    if step.get("target") is not None:
        try:
            return _resolve_target(step["target"])
        except ToolError:
            return None
    path = step.get("path")
    if isinstance(path, str) and path:
        return _window_for_atspi_app(path.split("/")[0])
    return None


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
    from .server import HANDLERS  # late: server imports this module
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError('steps must be a non-empty list, e.g. '
                        '[{"do":"click","x":100,"y":200},{"do":"key","target":"gedit",'
                        '"combo":"ctrl+s"}]', code="bad_args")
    if len(steps) > DO_STEPS_MAX:
        raise ToolError(f"{len(steps)} steps is more than the {DO_STEPS_MAX} allowed "
                        "in one call; a sequence that long should be checked partway",
                        code="bad_args")

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
        if not isinstance(step, dict):
            continue
        first_hint = _step_window_hint(step)
        if first_hint:
            break
    first_point = None
    for step in steps:
        if isinstance(step, dict) and step.get("x") is not None:
            first_point = (float(step["x"]), float(step.get("y") or 0))
            break
    watching = _look_before(a, hint_window=first_hint, point=first_point)

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ToolError(f"step {index} is not an object", code="bad_args")
        verb = str(step.get("do") or "").strip().lower()
        if verb == "wait_ms":
            verb = "sleep"
        if verb == "sleep":
            millis = int(step.get("ms") or step.get("wait_ms") or 200)
            if not 0 < millis <= 10_000:
                raise ToolError("sleep ms must be between 1 and 10000",
                                code="bad_args")
            time.sleep(millis / 1000)
            done.append({"step": index, "do": "sleep", "ok": True, "ms": millis})
            continue

        if verb not in _STEP_VERBS:
            raise ToolError(f"step {index}: unknown do {verb!r}. Known: sleep, "
                            + ", ".join(sorted(_STEP_VERBS)), code="bad_args")
        tool_name, required = _STEP_VERBS[verb]
        missing = [k for k in required if step.get(k) is None]
        if missing:
            raise ToolError(f"step {index} ({verb}) needs {', '.join(missing)}",
                            code="bad_args")

        args = {k: v for k, v in step.items() if k != "do"}
        args["look"] = False            # one picture per call, not one per step
        try:
            out = HANDLERS[tool_name](args)
            done.append({"step": index, "do": verb, "ok": True,
                         "detail": _step_detail(out)})
            if isinstance(out, dict) and isinstance(out.get("window"), dict):
                last_window = out["window"]
            elif step.get("target") is not None or step.get("path") is not None:
                last_window = _step_window_hint(step) or last_window
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

    if (watching.mode is not False and last_window
            and (not watching.window
                 or watching.window.get("id") != last_window.get("id"))):
        # The sequence moved to a different window than the one measured at the
        # start. Look at where it ended up, and say nothing about "changed".
        #
        # The mode guard is load-bearing: this used to substitute "window" for a
        # False mode while re-scoping, so `look: false` still attached a picture
        # -- the one setting whose entire job is to attach nothing.
        watching = _Look(watching.mode,
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
