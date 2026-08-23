from __future__ import annotations

import json
import time
from typing import Any

from .atspi import _window_for_atspi_app
from .capture import _Look, _look, _look_before, _parse_region
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
SLEEP_MAX_MS = 60_000

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
    "wait_for": ("wait_for", ("condition",)),
}

# Mirrors tool_wait_for (wcu/shell.py). Duplicated here so a bad condition is
# refused before step 1 runs instead of by the wait_for handler mid-sequence;
# if a condition is added there, add it here or the step form cannot use it.
_WAIT_CONDITIONS = {"focus_changes", "window_exists", "window_focused",
                    "window_gone"}
_SLEEP_KEYS = {"do", "ms", "wait_ms"}


def _canonical_verb(step: dict) -> str:
    verb = str(step.get("do") or "").strip().lower()
    return "sleep" if verb == "wait_ms" else verb


def _sleep_ms(step: dict, index: int) -> int:
    raw = step.get("ms") or step.get("wait_ms") or 200
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"step {index} (sleep): ms must be a number of "
                        f"milliseconds, not {raw!r}", code="bad_args") from None
    if not 0 < millis <= SLEEP_MAX_MS:
        raise ToolError(
            f"step {index} (sleep): ms must be between 1 and {SLEEP_MAX_MS}. "
            'A wait that long should be a condition, not a duration: use a '
            '{"do": "wait_for", "condition": ...} step instead',
            code="bad_args")
    return millis


def _validate_step(index: int, step: Any, schemas: dict[str, dict]) -> None:
    """Everything checkable about one step without executing it."""
    if not isinstance(step, dict):
        raise ToolError(f"step {index} is not an object", code="bad_args")
    verb = _canonical_verb(step)
    if verb == "sleep":
        _sleep_ms(step, index)
        unknown = sorted(set(step) - _SLEEP_KEYS)
        if unknown:
            raise ToolError(f"step {index} (sleep) has unknown "
                            f"key(s): {', '.join(unknown)}. Allowed: do, ms",
                            code="bad_args")
        return
    if verb not in _STEP_VERBS:
        raise ToolError(f"step {index}: unknown do {verb!r}. Known: sleep, "
                        + ", ".join(sorted(_STEP_VERBS)), code="bad_args")

    tool_name, required = _STEP_VERBS[verb]
    schema = schemas.get(tool_name) or {}
    need = set(required) | set(schema.get("required") or ())
    missing = sorted(k for k in need if step.get(k) is None)
    if missing:
        raise ToolError(f"step {index} ({verb}) needs {', '.join(missing)}",
                        code="bad_args")
    allowed = set(schema.get("properties") or {}) | {"do"}
    unknown = sorted(set(step) - allowed)
    if unknown:
        raise ToolError(
            f"step {index} ({verb}) has unknown key(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}", code="bad_args")

    if tool_name == "wait_for":
        condition = str(step.get("condition") or "").strip()
        if condition not in _WAIT_CONDITIONS:
            raise ToolError(f"step {index} ({verb}): condition must be one of: "
                            + ", ".join(sorted(_WAIT_CONDITIONS)),
                            code="bad_args")
        if condition != "focus_changes" and step.get("target") is None:
            raise ToolError(f"step {index} ({verb}): {condition} needs a "
                            "target window", code="bad_args")
        timeout = step.get("timeout")
        if timeout:                 # falsy timeout means the handler's default
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                raise ToolError(f"step {index} ({verb}): timeout must be a "
                                "number of seconds", code="bad_args") from None
            if not 0.2 <= timeout <= 120:
                raise ToolError(f"step {index} ({verb}): timeout must be "
                                "between 0.2 and 120 seconds", code="bad_args")


def _validate_sequence(a: dict, steps: list) -> None:
    """Refuse the whole call before any step has run.

    A malformed step 4 used to fire steps 1-3 first, and a half-executed
    sequence is the worst outcome an autonomous run can produce: the desktop
    is left in a state nobody asked for and nobody looked at. Never start
    what cannot finish.
    """
    from .server import TOOLS  # late: server imports this module
    schemas = {t["name"]: t.get("inputSchema") or {} for t in TOOLS}

    look = a.get("look", "auto")
    if look not in (True, False, "none", "auto", "window", "screen", "region"):
        raise ToolError("look must be auto, window, screen, region, or false",
                        code="bad_args")
    if look == "region":
        region = a.get("look_at") or a.get("region")
        if region is None:
            raise ToolError('look:"region" needs look_at: {x, y, width, '
                            'height} or [x, y, width, height]', code="bad_args")
        _parse_region(region)               # raises bad_args if malformed

    for index, step in enumerate(steps):
        _validate_step(index, step, schemas)


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
    _validate_sequence(a, steps)

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

    # _validate_sequence already refused anything malformed, so nothing past
    # this point raises bad_args: a run that starts can reach its end.
    for index, step in enumerate(steps):
        verb = _canonical_verb(step)
        if verb == "sleep":
            millis = _sleep_ms(step, index)
            time.sleep(millis / 1000)
            done.append({"step": index, "do": "sleep", "ok": True, "ms": millis})
            continue

        tool_name, _ = _STEP_VERBS[verb]
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

    if (watching.mode in ("auto", "window") and last_window
            and (not watching.window
                 or watching.window.get("id") != last_window.get("id"))):
        # The sequence moved to a different window than the one measured at the
        # start. Look at where it ended up, and say nothing about "changed".
        #
        # The mode guard is load-bearing twice over. It used to be `is not
        # False`, which re-scoped every mode: `look: false` got a picture
        # attached -- the one setting whose entire job is to attach nothing --
        # and `look: "region"` had its look_at rectangle silently replaced by
        # the last window's geometry, because region mode carries no
        # watching.window and so always looked "unscoped" here. An explicit
        # region or screen choice is the caller's to make; only the modes that
        # left the scope to us ("auto", "window") may be re-scoped.
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
