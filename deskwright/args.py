"""Read what an agent obviously meant, and say that it was read that way.

Transcripts from 2026-08 to 2026-11 (about 80 Claude and 6 Codex sessions)
show the same refusals again and again, none of them a real disagreement
about what to do:

  * five names for "which window" across the tools -- window, window_id,
    target, expect_window -- so `screenshot(window_id=...)`,
    `ui_snapshot(window=...)` and `press_keys(window=...)` were refused;
  * `look: "false"` as a string, refused six times in one session on
    2026-11-08 by a host that stringifies arguments;
  * `look_at` or `region` sent as a JSON string, refused the same way;
  * `pointer_scroll(direction="down", amount=5)`, the shape its own
    description used to advertise;
  * `seconds: "4"` and other numbers sent as strings.

Each of these is fixed up here, BEFORE validation, and every fix is reported
back to the caller in `argument_notes` so a wrong guess is still visible.
Anything not on this list is refused as before, with the closest valid
argument names in the message.
"""
from __future__ import annotations

import difflib
import json
from typing import Any

from .errors import ToolError

WINDOW_NAMES = ("window", "window_id", "target", "expect_window")

# Tool-specific synonyms that were seen in transcripts. Only names with one
# unambiguous meaning belong here.
SYNONYMS: dict[str, dict[str, str]] = {
    "ui_find": {"query": "text", "name": "text"},
    "press_keys": {"keys": "combo", "key": "combo", "shortcut": "combo"},
    "type_text": {"string": "text", "value": "text"},
    "find_text": {"query": "text"},
    "launch_app": {"app": "desktop_id", "argv": "command"},
    "hold_key": {"duration": "seconds", "combo": "key"},
}

_SCROLL_DIRECTIONS = {"down": ("dy", 1), "up": ("dy", -1),
                      "right": ("dx", 1), "left": ("dx", -1)}


def _types(schema: dict) -> set[str]:
    """Every JSON type a property accepts."""
    out = set()
    if isinstance(schema.get("type"), str):
        out.add(schema["type"])
    elif isinstance(schema.get("type"), list):
        out.update(schema["type"])
    for alt in schema.get("anyOf") or ():
        out |= _types(alt)
    return out


def _coerce(value: Any, schema: dict) -> tuple[Any, bool]:
    """A string that is plainly a boolean, number, object or array, typed."""
    if not isinstance(value, str):
        return value, False
    kinds = _types(schema)
    text = value.strip()
    enum = schema.get("enum") or [e for alt in schema.get("anyOf") or ()
                                  for e in alt.get("enum") or ()]
    if value in enum:
        return value, False
    if "boolean" in kinds and text.lower() in ("true", "false"):
        return text.lower() == "true", True
    if kinds & {"integer", "number"} and "string" not in kinds:
        try:
            number = float(text)
        except ValueError:
            return value, False
        if "integer" in kinds and number.is_integer():
            return int(number), True
        if "number" in kinds:
            return number, True
        return value, False
    if kinds & {"object", "array"} and text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            return value, False
        if ("object" in kinds and isinstance(parsed, dict)) or \
                ("array" in kinds and isinstance(parsed, list)):
            return parsed, True
    return value, False


def _window_id(value: Any) -> Any:
    """`window_id` is integer-only; a name or a digit string is resolved."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, str) and value.strip():
        from .shell import _resolve_target
        return _resolve_target(value)["id"]
    return value


def normalize(tool: str, args: dict, schema: dict) -> list[str]:
    """Rewrite `args` in place to what the caller meant; return the notes."""
    props = schema.get("properties") or {}
    notes: list[str] = []

    # 1. One name for "which window", whichever of the four was sent.
    present = [n for n in WINDOW_NAMES if n in props]
    if len(present) == 1:
        canonical = present[0]
        for alias in WINDOW_NAMES:
            if alias != canonical and alias in args and alias not in props:
                if canonical in args:
                    raise ToolError(
                        f"{tool}: got both {alias} and {canonical}; this tool calls "
                        f"the window {canonical}", code="bad_args")
                args[canonical] = args.pop(alias)
                notes.append(f"{alias} was read as {canonical}")
    if "window_id" in props and "window_id" in args:
        resolved = _window_id(args["window_id"])
        if resolved != args["window_id"]:
            notes.append(f"window_id {args['window_id']!r} resolved to {resolved}")
            args["window_id"] = resolved

    # 2. Synonyms seen in transcripts.
    for alias, canonical in SYNONYMS.get(tool, {}).items():
        if alias in args and alias not in props and canonical in props \
                and canonical not in args:
            args[canonical] = args.pop(alias)
            notes.append(f"{alias} was read as {canonical}")

    # 3. pointer_scroll(direction=..., amount=...) -> dy/dx wheel clicks.
    if tool == "pointer_scroll" and "direction" in args and "direction" not in props:
        direction = str(args.pop("direction")).strip().lower()
        if direction not in _SCROLL_DIRECTIONS:
            raise ToolError("pointer_scroll direction must be up, down, left or "
                            "right (or give dy/dx in wheel clicks)", code="bad_args")
        amount = args.pop("amount", args.pop("clicks", 3))
        try:
            amount = abs(int(float(amount)))
        except (TypeError, ValueError):
            raise ToolError("pointer_scroll amount is a number of wheel clicks",
                            code="bad_args") from None
        axis, sign = _SCROLL_DIRECTIONS[direction]
        if args.get(axis):
            raise ToolError(f"pointer_scroll: got both direction and {axis}",
                            code="bad_args")
        args[axis] = sign * amount
        notes.append(f"direction={direction!r} amount={amount} was read as "
                     f"{axis}={sign * amount} wheel clicks")

    # 4. Strings that are plainly another JSON type.
    for name in list(args):
        if name in props:
            value, changed = _coerce(args[name], props[name])
            if changed:
                notes.append(f"{name} {args[name]!r} was read as {json.dumps(value)}")
                args[name] = value
    return notes


def unknown_message(tool: str, unknown: set[str], props: dict) -> str:
    """Name each unknown argument's closest valid neighbour, then list them all."""
    hints = []
    for name in sorted(unknown):
        close = difflib.get_close_matches(name, list(props), n=1, cutoff=0.55)
        if close:
            hints.append(f"{name} -> did you mean {close[0]}?")
        elif tool == "pointer_scroll" and name in ("amount", "pixels", "delta"):
            hints.append(f"{name} -> use dy (down is positive) or dx, counted in "
                         "wheel clicks, not pixels")
        else:
            hints.append(f"{name} -> no such argument")
    valid = ", ".join(sorted(p for p in props if p != "observation_mode"))
    return (f"{tool}: unknown arguments {sorted(unknown)}: {'; '.join(hints)}. "
            f"Valid arguments: {valid}")
