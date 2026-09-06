"""Continuous pointer paths, independent of a particular desktop backend."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from .errors import ToolError

PATH_MAX_POINTS = 2048
PATH_MAX_MS = 15_000


def validate_path(a: dict) -> tuple[list[tuple[float, float]], float, str]:
    raw = a.get("points")
    if not isinstance(raw, list) or not 2 <= len(raw) <= PATH_MAX_POINTS:
        raise ToolError(f"points needs 2-{PATH_MAX_POINTS} [x, y] pairs", code="bad_args")
    points = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in point
        ):
            raise ToolError("every path point must be two finite numbers", code="bad_args")
        points.append((float(point[0]), float(point[1])))
    duration = a.get("duration_ms", 1000)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not (
        50 <= duration <= PATH_MAX_MS
    ):
        raise ToolError(f"duration_ms must be 50-{PATH_MAX_MS}", code="bad_args")
    button = a.get("button", "left")
    if button not in ("left", "middle", "right"):
        raise ToolError("button must be left, middle or right", code="bad_args")
    if a.get("target") is None:
        raise ToolError("target window is required for a continuous path", code="bad_args")
    if not any(p != points[0] for p in points[1:]):
        raise ToolError("path must contain some motion", code="bad_args")
    return points, float(duration) / 1000, button


def execute_path(pointer: Any, points: list[tuple[float, float]], duration: float,
                 button: str, check: Callable[[float, float], None], *,
                 clock: Callable = time.monotonic, sleep: Callable = time.sleep) -> dict:
    """Visit every vertex, interpolate at ~120Hz, release even on cancellation.

    Timings are deadlines, not a fixed sleep added to every backend call.
    A slow backend still visits every vertex; the reported elapsed time exposes
    any slowdown instead of silently cutting corners in a drawing.
    """
    lengths = [math.dist(a, b) for a, b in pairwise(points)]
    total = sum(lengths)
    check(*points[0])
    pointer.move_to(*points[0])
    # Match the measured arrival delay of Gestures.drag. Without it the
    # compositor can deliver button-down to the previous pointer surface;
    # the Wayland GTK witness observed no stroke at all (2026-09-06).
    sleep(0.06)
    # On a newly created RemoteDesktop device, the first motion can precede
    # the receiver's enter event. Reassert arrival; never retry the stroke.
    pointer.move_to(*points[0])
    sleep(0.02)
    check(*points[0])
    started = clock()
    next_check = started + 0.1
    travelled = 0.0
    events = 0
    try:
        pointer.button(button, True)
        for a, b, length in zip(points, points[1:], lengths, strict=False):
            segment_time = duration * length / total
            count = max(1, math.ceil(segment_time * 120))
            for i in range(1, count + 1):
                fraction = i / count
                x = a[0] + (b[0] - a[0]) * fraction
                y = a[1] + (b[1] - a[1]) * fraction
                deadline = started + duration * (travelled + length * fraction) / total
                # Check while waiting too, so a long segment remains interruptible.
                while deadline > clock():
                    sleep(max(0, min(deadline - clock(), 0.05)))
                    if clock() >= next_check:
                        check(x, y)
                        next_check = clock() + 0.1
                if clock() >= next_check:
                    check(x, y)
                    next_check = clock() + 0.1
                pointer.move_to(x, y)
                events += 1
            travelled += length
        # Let the receiver consume the last motion before the release.
        sleep(0.06)
    finally:
        try:
            pointer.button(button, False)
        except Exception:
            # Closing the compositor session releases its held buttons even
            # when the explicit release itself could not be delivered.
            pointer.stop()
            raise
    return {"points": len(points), "motion_events": events,
            "requested_ms": round(duration * 1000),
            "arrival_settle_ms": 80,
            "elapsed_ms": round((clock() - started) * 1000),
            "button": button, "from": list(points[0]), "to": list(points[-1])}
