from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import HERE
from .errors import ToolError
from .shell import _gdbus, _resolve_target, extension_methods, list_windows, window_at

SHOT_CACHE = Path(os.path.expanduser("~/.cache/wayland-computer-use/shots"))
SHOT_CACHE_KEEP = 40


def _shot_path(a: dict) -> tuple[Path, bool]:
    """Where the PNG goes. Naming one is now optional, because inventing a
    throwaway /tmp path was pure ceremony on every single call."""
    raw = str(a.get("path") or "").strip()
    if not raw:
        SHOT_CACHE.mkdir(parents=True, exist_ok=True)
        _prune_shot_cache()
        return SHOT_CACHE / f"shot-{time.time():.3f}.png", False

    path = Path(os.path.expanduser(raw)).absolute()
    if path.is_dir():
        raise ToolError(f"{path} is a directory; give a file path ending in .png",
                        code="bad_args")
    # The suffix check is load-bearing, not cosmetic: this used to unlink whatever
    # already existed at the caller-supplied path before capturing, so
    # screenshot{"path": "~/system/healthcheck.sh"} deleted that file -- and if the
    # capture then failed (a locked screen is enough), it was simply gone.
    if path.suffix.lower() != ".png":
        raise ToolError(f"path must end in .png, got {path.name!r}", code="bad_args")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, True


def _prune_shot_cache() -> None:
    try:
        shots = sorted(SHOT_CACHE.glob("shot-*.png"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-SHOT_CACHE_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:                                       # pragma: no cover
        pass                                                # housekeeping only


def _capture(path: Path, region: tuple[int, int, int, int] | None = None,
             include_cursor: bool = False) -> bool:
    """Put a PNG of `region` (or the whole screen) at `path`.

    Returns whether gnome-shell did the cropping. Capture goes to a temp file
    beside the target and is renamed on success, so an existing file is only
    ever replaced by a real screenshot.
    """
    tmp = path.with_name(f".{path.name}.capturing")
    tmp.unlink(missing_ok=True)
    try:
        if region and "ScreenshotArea" in extension_methods():
            _gdbus("ScreenshotArea", *(str(int(v)) for v in region), str(tmp))
            cropped_in_shell = True
        else:
            _gdbus("Screenshot", str(tmp), "true" if include_cursor else "false")
            cropped_in_shell = False
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ToolError("the call returned but no image was written",
                            code="capture_failed")
        with tmp.open("rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ToolError("the capture was written but is not a PNG",
                                code="capture_failed")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return cropped_in_shell


def _occlusion(window: dict) -> dict | None:
    """How much of `window` is covered by windows above it in the stack.

    Captures come from the screen, so a covered window returns whatever is in
    front of it and looks exactly like the target having the wrong content. That
    cost a wasted capture and a wasted Read on 2026-08-20; saying so is free.
    """
    try:
        windows = list_windows()
    except ToolError:
        return None
    seen, above = False, []
    for w in windows:                                       # bottom of stack first
        if w["id"] == window["id"]:
            seen = True
            continue
        if seen and not w.get("minimized"):
            above.append(w)
    if not seen or not above:
        return None

    area = max(1, window["width"] * window["height"])
    covered = 0
    for w in above:
        overlap_w = min(window["x"] + window["width"], w["x"] + w["width"]) - max(window["x"], w["x"])
        overlap_h = min(window["y"] + window["height"], w["y"] + w["height"]) - max(window["y"], w["y"])
        if overlap_w > 0 and overlap_h > 0:
            covered += overlap_w * overlap_h
    if covered <= 0:
        return None
    percent = round(min(100.0, 100.0 * covered / area), 1)
    return {
        "occluded_percent": percent,
        "by": [w["wm_class"] for w in above][:4],
        "detail": (f"{percent}% of this window is behind another one, so the capture "
                   "shows what is in front of it. activate_window first if that is "
                   "not what you wanted."),
    }


def tool_screenshot(a: dict) -> dict:
    path, persistent = _shot_path(a)
    region, window = _screenshot_region(a)

    cropped_in_shell = _capture(path, region, bool(a.get("include_cursor")))

    result: dict[str, Any] = {"path": str(path)}
    if not persistent:
        result["path_note"] = "kept in the shot cache; pass path= to keep it somewhere"
    origin = (region[0], region[1]) if region else (0, 0)
    if region and not cropped_in_shell:
        _crop(path, region)
        result["cropped_by"] = "this server, after a full capture"
    if region:
        result["region"] = {"x": region[0], "y": region[1],
                            "width": region[2], "height": region[3]}
    if window is not None:
        occlusion = _occlusion(window)
        if occlusion:
            result["occluded"] = occlusion

    scale = float(a.get("scale") or 1.0)
    if not 0.05 <= scale <= 4.0:
        raise ToolError("scale must be between 0.05 and 4.0", code="bad_args")

    annotate = a.get("annotate")
    inline = a.get("inline", True)

    # When the image is being handed over inline, resizing the PNG first is
    # wasted work -- measured 0.51s for scale:0.5 versus 0.32s for the full
    # capture, because it costs a decode and a PNG re-encode. The inline encoder
    # has to resize anyway, so scale is folded into its target size instead and
    # the file on disk stays at native resolution.
    if scale != 1.0 and not inline:
        _rescale(path, min(scale, 1.0))
        result["scale"] = scale

    # Annotation goes on at native resolution and is labelled in SCREEN
    # coordinates, so it survives whatever the image is scaled to afterwards.
    if annotate:
        result["annotated"] = _annotate(path, origin, annotate, a,
                                        1.0 if inline else min(scale, 1.0))

    native = _png_dimensions(path)
    result["bytes"] = path.stat().st_size
    result["dimensions"] = native

    if inline:
        native_long = max(_dimension_pair(native) or (MODEL_MAX_EDGE, 0))
        target_edge = max(16, min(MODEL_MAX_EDGE, int(native_long * scale)))
        _attach_inline(result, path, {**a, "max_edge": target_edge,
                                      "upscale": scale > 1.0})
        shown = result.get("shown")
        if isinstance(shown, dict):
            result["coordinate_note"] = _coordinate_note(shown["dimensions"],
                                                         native, origin)
    elif region:
        result["coordinate_note"] = (
            f"pixel (px, py) in this image is screen "
            f"({origin[0]} + px, {origin[1]} + py)."
        )
    return result


def _dimension_pair(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d+)x(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _coordinate_note(shown: str, native: str, origin: tuple[int, int]) -> str:
    """How to turn a pixel in the image the model is looking at into a click.

    This has to describe the IMAGE THAT WAS SENT, not the file on disk. They are
    different sizes now, and a note about the wrong one is worse than no note:
    it produces confident clicks in the wrong place.
    """
    shown_wh, native_wh = _dimension_pair(shown), _dimension_pair(native)
    ox, oy = origin
    if not shown_wh or not native_wh:                       # pragma: no cover
        return f"this image starts at screen ({ox}, {oy})."
    factor = native_wh[0] / shown_wh[0] if shown_wh[0] else 1.0
    if abs(factor - 1.0) < 0.005:
        if not ox and not oy:
            return "this image is 1:1 with the screen; pixel (px, py) is screen (px, py)."
        return f"pixel (px, py) in this image is screen ({ox} + px, {oy} + py)."
    return (f"this image is {shown} for a {native} area, so pixel (px, py) is screen "
            f"({ox} + px*{factor:.3f}, {oy} + py*{factor:.3f}). "
            "Any drawn labels are already in screen coordinates.")


def _png_dimensions(path: Path) -> str:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return f"{img.width}x{img.height}"
    except Exception:
        pass
    if shutil.which("file"):
        out = subprocess.run(["file", "-b", str(path)], capture_output=True,
                             text=True, timeout=15).stdout
        m = re.search(r"(\d+) x (\d+)", out)
        if m:
            return f"{m.group(1)}x{m.group(2)}"
    return ""


def _pillow():
    try:
        from PIL import Image, ImageDraw
    except Exception:                                       # pragma: no cover
        raise ToolError(
            "this needs Pillow (python3-pil) for image work and it is not installed",
            code="capture_failed",
        ) from None
    return Image, ImageDraw


# =========================================================================
# handing an image to the model instead of a path to one
# =========================================================================
#
# Measured on this machine, 2026-08-22, from real session transcripts: a
# screenshot that returns a path is followed by a Read of that path 61 times out
# of 62, and screenshot->Read->next-action takes a median of 14.0s. The same
# session's browser tool, which returns the image inline, went screenshot->next
# action in 7.9s. The capture itself is 0.23s. So essentially all of the cost of
# "look at the screen" is the extra model round trip, and removing it is worth
# more than every other optimisation in this file combined.
#
# The size rules are not arbitrary. Anthropic downscales any image whose long
# edge exceeds 1568px and bills roughly width*height/750 tokens, so sending a
# 1920x1080 PNG pays for a resize that happens anyway. Doing it here costs 40ms
# and turns 1843 tokens into what the caller actually needs.

MODEL_MAX_EDGE = 1568      # above this the API downscales anyway
INLINE_QUALITY = 75        # JPEG; 60 starts to smear small UI text
INLINE_MAX_BYTES = 3_500_000
_INLINE_KEY = "__inline_image__"


def _estimate_tokens(width: int, height: int) -> int:
    return int(width * height / 750)


def _encode_inline(path: Path, max_edge: int = MODEL_MAX_EDGE,
                   quality: int = INLINE_QUALITY, upscale: bool = False) -> dict:
    """A PNG on disk, as a JPEG the model can be handed directly.

    RGBA -> RGB is required, not cosmetic: the extension writes RGBA and JPEG
    has no alpha channel, so this raises OSError without it.

    LANCZOS rather than BILINEAR: this is a picture of TEXT, and the whole point
    is to still be able to read a menu item after the resize. Measured at 40ms
    for a full screen, which is not worth trading legibility for.
    """
    import base64
    Image, _ = _pillow()
    with Image.open(path) as img:
        img = img.convert("RGB")
        width, height = img.size
        longest = max(width, height)
        if longest > max_edge or (upscale and longest < max_edge):
            factor = max_edge / longest
            width, height = max(1, int(width * factor)), max(1, int(height * factor))
            img = img.resize((width, height), Image.LANCZOS)

        import io
        for attempt_quality in (quality, 55, 40):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=attempt_quality)
            raw = buf.getvalue()
            if len(raw) <= INLINE_MAX_BYTES:
                break
        else:                                               # pragma: no cover
            raise ToolError("this image will not compress to a sane size",
                            code="capture_failed")

    return {
        "data": base64.b64encode(raw).decode("ascii"),
        "media_type": "image/jpeg",
        "dimensions": f"{width}x{height}",
        "bytes": len(raw),
        "tokens_estimate": _estimate_tokens(width, height),
        "quality": attempt_quality,
    }


# =========================================================================
# watching the screen settle, and telling a hit from a miss
# =========================================================================
#
# This exists because `pointer_click` reports focus:"unchanged" for both a
# successful press of a button that never takes focus (every Telegram inline
# button) and a click that hit dead space. They were indistinguishable without a
# second capture and a human look; now they differ by a number.
#
# Which number matters. Measured on a calculator window, 240x135 cells,
# 2026-08-22 -- the first two columns are what a "percent of cells that moved"
# metric sees, the last two are what this uses instead:
#
#                       cells>12   percent   cells>60   max delta
#   nothing happening          0     0.00%          0           0
#   click into dead space      0     0.00%          0          10
#   button press: clear       24     0.07%         16         180
#   button press: digit 7     16     0.05%         10         196
#   button press: digit 5     24     0.07%         15         173
#
# A percentage cannot separate those: a real button press moves 0.05% of a
# window and the first threshold tried (0.5%) called every one of them "nothing
# happened" -- confidently, in the result the model reads. The strong-cell count
# separates them by an order of magnitude, because UI changes are small in area
# and enormous in contrast: text appears, a button lights up, a menu opens.

FINGERPRINT = (240, 135)     # 32400 cells; ~8ms to compare in pure Python
CELL_NOISE = 12              # 0-255; below this is encoder and antialiasing jitter
STRONG_DELTA = 60            # a cell this different is new content, not jitter
STRONG_CELLS_STABLE = 2      # a blinking caret is one or two cells; ignore it
# Measured margin, gnome-calculator, 2026-08-22: a blinking text caret peaks at
# 11 strong cells on a completely idle window (it cycles 0 -> 11 -> 0 about once
# a second), while the smallest real button press moves 22. 12 sits between them.
# Blinkers that toggle during the settle window are masked out below, which is
# what makes this margin comfortable rather than lucky.
STRONG_CELLS_CHANGED = 12
STABLE_CHANGED_PCT = 0.1     # two frames this close are the same frame
CHANGE_FLOOR_PCT = 0.06      # backstop for a large, low-contrast change
SETTLE_MAX_S = 1.5
SETTLE_POLL_S = 0.12
SETTLE_MIN_FRAMES = 4       # enough of a window to see what blinks on its own


def _fingerprint(path: Path) -> bytes:
    """A greyscale thumbnail, as bytes. Cheap enough to take every frame."""
    Image, _ = _pillow()
    with Image.open(path) as img:
        return img.convert("L").resize(FINGERPRINT, Image.BOX).tobytes()


def _frame_delta(before: bytes, after: bytes, ignore: set[int] | None = None) -> dict:
    """How different two frames are, in the three ways that turn out to matter.

    `ignore` is the set of cells known to be moving on their own -- a caret, a
    spinner, a clock. They are excluded from the counts rather than from the
    picture.
    """
    if len(before) != len(after) or not before:
        return {"percent": 100.0, "strong_cells": len(after or b""), "max_delta": 255}
    moved = strong = peak = 0
    for index, (x, y) in enumerate(zip(before, after)):
        if ignore and index in ignore:
            continue
        delta = x - y if x > y else y - x
        if delta > CELL_NOISE:
            moved += 1
            if delta > STRONG_DELTA:
                strong += 1
        if delta > peak:
            peak = delta
    return {"percent": round(100.0 * moved / len(before), 3),
            "strong_cells": strong, "max_delta": peak}


def _ambient_cells(frames: list[bytes]) -> set[int]:
    """Cells that were moving by themselves while nothing was happening.

    A caret blinks on a roughly one-second cycle, so a two-frame baseline taken
    120ms apart usually catches one phase and calls the other phase a change --
    measured: 11 strong cells on an idle calculator, six times out of six. The
    settle loop is already sampling frames after the action; anything that
    changes BETWEEN those samples is something that changes on its own, and is
    not evidence that the action did anything.
    """
    moving: set[int] = set()
    for earlier, later in zip(frames, frames[1:]):
        if len(earlier) != len(later):
            continue
        for index, (x, y) in enumerate(zip(earlier, later)):
            if abs(x - y) > CELL_NOISE:
                moving.add(index)
    return moving


def _frame_change(before: bytes, after: bytes) -> float:
    """Percent of cells that moved more than sensor noise. Kept for callers that
    only want the one number (the settle loop's noise figure)."""
    return _frame_delta(before, after)["percent"]


def _changed_since(baseline: list[bytes], current: bytes,
                   ambient: set[int] | None = None,
                   floor_percent: float = CHANGE_FLOOR_PCT) -> dict:
    """Whether the screen differs from `baseline` because something HAPPENED.

    Three rules, each of which exists because the two simpler ones were tried
    and measured wrong on this machine:

      * strong cells OUTSIDE anything that was already moving, threshold 4.
        The cheap, confident case: a menu opened, a dialog appeared.
      * strong cells including them, threshold 12. Needed because a caret and
        the text it sits next to occupy the SAME cells -- masking the caret out
        masked the typed digit out with it, and a real button press dropped from
        22 strong cells to 0. 12 sits above the caret's measured peak of 11.
      * a large, high-contrast area change, for a scroll or a page swap, which
        moves a lot of cells without any of them being new content.

    The area rule needs the contrast test beside it: a click that only moved
    focus repainted 2.6% of a window with a peak difference of 17/255, and an
    area-only rule called that a change.
    """
    outside = _least_change(baseline, current, ambient) if ambient else None
    overall = _least_change(baseline, current)
    delta = dict(overall)
    if outside is not None:
        delta["strong_cells_excluding_moving"] = outside["strong_cells"]

    landed = (
        (outside is not None and outside["strong_cells"] >= 4)
        or overall["strong_cells"] >= STRONG_CELLS_CHANGED
        or (overall["percent"] > max(floor_percent, 1.0) and overall["max_delta"] > 40)
    )
    delta["landed"] = landed
    return delta


def _settle(path: Path, region: tuple[int, int, int, int] | None,
            max_wait: float = SETTLE_MAX_S) -> dict:
    """Capture until two consecutive frames agree, then return the last one.

    A fixed sleep is either too short (and catches a half-drawn menu) or too long
    (and is pure latency). Foreground `sleep` is blocked on this machine anyway.
    """
    start = time.monotonic()
    seen: list[bytes] = []
    noise = 0.0
    settled_at: int | None = None
    while True:
        _capture(path, region)
        current = _fingerprint(path)
        seen.append(current)
        waited = time.monotonic() - start
        if len(seen) >= 2:
            delta = _frame_delta(seen[-2], current)
            noise = delta["percent"]
            if (delta["strong_cells"] <= STRONG_CELLS_STABLE
                    and noise <= STABLE_CHANGED_PCT):
                settled_at = len(seen)
        # Keep sampling past the first agreement, up to SETTLE_MIN_FRAMES, so
        # there is enough of a window to see what moves on its own. Two frames
        # 120ms apart cannot tell a caret from a keystroke.
        if settled_at is not None and len(seen) >= SETTLE_MIN_FRAMES:
            # The delta between the frames that agreed IS the live noise floor
            # for this exact rectangle. Reporting it lets the verdict adapt
            # instead of trusting one constant for a dialog and a whole desktop.
            return {"settled": True, "frames": len(seen), "noise_percent": noise,
                    "waited_seconds": round(waited, 2), "fingerprint": current,
                    "ambient": _ambient_cells(seen)}
        if waited >= max_wait:
            return {"settled": settled_at is not None, "frames": len(seen),
                    "noise_percent": noise, "waited_seconds": round(waited, 2),
                    "fingerprint": current, "ambient": _ambient_cells(seen),
                    "detail": None if settled_at is not None else
                    "still changing when time ran out; the image is the last frame"}
        time.sleep(SETTLE_POLL_S)


def _look_region(a: dict, hint_window: dict | None,
                 point: tuple[float, float] | None) -> tuple[Any, tuple | None, dict | None]:
    """Resolve `look` into a rectangle. Returns (mode, region, window)."""
    look = a.get("look", "auto")
    if look is False or look == "none":
        return False, None, None
    if look is True:
        look = "auto"
    if look not in ("auto", "window", "screen", "region"):
        raise ToolError("look must be auto, window, screen, region, or false",
                        code="bad_args")

    if look == "screen":
        return look, None, None
    if look == "region":
        region = a.get("look_at") or a.get("region")
        if region is None:
            raise ToolError('look:"region" needs look_at: {x, y, width, height}',
                            code="bad_args")
        return look, _parse_region(region), None

    window = hint_window
    if window is None and a.get("expect_window") is not None:
        try:
            window = _resolve_target(a["expect_window"])
        except ToolError:
            window = None
    if window is None and point is not None:
        try:
            hit = window_at(point[0], point[1])
            # window_at answers {"window": {...}, "covering": [...]}. The
            # compositor's pick is the authority on WHICH window, but it carries
            # no geometry -- only id, class, title, focused. Taking it at face
            # value left every click looking at the whole screen instead of the
            # window it hit: 6x the tokens, 7x the wall clock, and a change
            # figure diluted until a real hit read as a miss.
            covering = hit.get("covering") or []
            picked = hit.get("window") or {}
            window = next((w for w in covering if w["id"] == picked.get("id")), None)
            if window is None:
                window = covering[0] if covering else None
        except ToolError:
            window = None
    if not window or window.get("width") is None:
        return look, None, None                             # fall back to the screen
    return look, (window["x"], window["y"], window["width"], window["height"]), window


class _Look:
    """The before-half of a look, carried across the action to the after-half.

    Resolving the rectangle costs two D-Bus round trips, so it is done once here
    rather than again on the way out.
    """

    __slots__ = ("mode", "region", "window", "before")

    def __init__(self, mode: Any, region: tuple | None, window: dict | None,
                 before: list[bytes] | None):
        self.mode, self.region, self.window, self.before = mode, region, window, before


def _baseline(path: Path, region: tuple[int, int, int, int] | None,
              gap: float = 0.12, frames: int = 2) -> list[bytes]:
    """Two frames of "before", a moment apart.

    One frame is not a baseline on a screen with anything blinking in it.
    gnome-calculator's entry has a caret, and a single-frame baseline reported a
    change 6 times out of 6 on a completely idle window: 5-11 strong cells,
    peaking at 207/255, purely from the caret being on in one frame and off in
    the other. Comparing against both frames and taking the smaller difference
    costs one extra capture -- 50ms for a window -- and makes a blink invisible
    while leaving real changes untouched.
    """
    out = []
    for index in range(max(2, frames)):
        if index:
            time.sleep(gap)
        _capture(path, region)
        out.append(_fingerprint(path))
    return out


def _least_change(baseline: list[bytes], current: bytes,
                  ignore: set[int] | None = None) -> dict:
    """How different `current` is from the baseline frame it resembles most."""
    deltas = [_frame_delta(b, current, ignore) for b in baseline if b]
    if not deltas:
        return {"percent": 0.0, "strong_cells": 0, "max_delta": 0}
    return min(deltas, key=lambda d: (d["strong_cells"], d["percent"]))


def _look_before(a: dict, hint_window: dict | None = None,
                 point: tuple[float, float] | None = None) -> _Look:
    """The screen as it was, so the action's effect can be measured."""
    try:
        mode, region, window = _look_region(a, hint_window, point)
    except ToolError:
        raise
    except Exception:                                       # pragma: no cover
        return _Look(False, None, None, None)
    if mode is False:
        return _Look(False, None, None, None)
    try:
        path, _ = _shot_path({})
        return _Look(mode, region, window, _baseline(path, region))
    except Exception:
        # Never let the measurement break the action it is measuring.
        return _Look(mode, region, window, None)


def _look(a: dict, result: dict, prepared: _Look) -> dict:
    """Attach what the screen looks like now, and how much the action changed it.

    `look:"auto"` (the default) always reports the change figure -- it costs one
    capture -- but only spends the tokens on an image when something actually
    moved. A click that changed nothing is the case you most need told about and
    the one you least need a picture of.
    """
    mode, region, window, before = (prepared.mode, prepared.region,
                                    prepared.window, prepared.before)
    if mode is False:
        return result

    path, _ = _shot_path({})
    settle = _settle(path, region, float(a.get("settle_max_s") or SETTLE_MAX_S))
    after = settle.pop("fingerprint")
    ambient = settle.pop("ambient", None) or set()

    view: dict[str, Any] = {"of": (window["wm_class"] if window else "whole screen"),
                            "settled": settle["settled"],
                            "frames": settle["frames"],
                            "waited_seconds": settle["waited_seconds"]}
    if settle.get("detail"):
        view["detail"] = settle["detail"]
    if ambient:
        view["moving_on_its_own"] = len(ambient)

    landed = None
    if before:
        floor = max(CHANGE_FLOOR_PCT, 2 * settle.get("noise_percent", 0.0))
        delta = _changed_since(before, after, ambient, floor)
        landed = delta.pop("landed")
        view["changed"] = delta
        view["verdict"] = (
            "the screen changed, so this landed on something"
            if landed else
            "NOTHING on screen changed -- if this was meant to press something, it missed"
        )

    show = mode != "auto" or landed is None or landed
    if show:
        origin = (region[0], region[1]) if region else (0, 0)
        native = _png_dimensions(path)
        _attach_inline(result, path, a)
        shown = result.get("shown")
        if isinstance(shown, dict):
            view["coordinate_note"] = _coordinate_note(shown["dimensions"], native, origin)
    else:
        view["image"] = "not attached: nothing changed, so there is nothing new to see"
    view["path"] = str(path)
    result["look"] = view
    return result


def _look_typed(a: dict, result: dict, window: dict, prepared: _Look) -> dict:
    """Prove typing landed with a picture, when AT-SPI readback is not available.

    Telegram, Chrome and Electron expose no editable text widget, so `type_text`
    returned verified:false and the caller spent a screenshot plus an image Read
    proving 20 characters arrived -- three sessions running, 12 wasted calls in
    one of them. The picture is the proof, so take it here rather than making
    the caller ask for it.
    """
    if prepared.mode is False:
        return result
    forced = {**a, "look": "window" if a.get("look", "auto") == "auto" else a["look"]}
    result = _look(forced, result, prepared)
    view = result.get("look")
    if isinstance(view, dict):
        view["why"] = ("attached because AT-SPI could not read the text back; "
                       "this picture is the only proof the characters arrived")
    return result


def _attach_inline(result: dict, path: Path, a: dict) -> dict:
    """Put the picture in the result unless the caller said not to."""
    if not a.get("inline", True):
        result["inline"] = False
        return result
    try:
        image = _encode_inline(
            path,
            max_edge=int(a.get("max_edge") or MODEL_MAX_EDGE),
            quality=int(a.get("quality") or INLINE_QUALITY),
            upscale=bool(a.get("upscale")),
        )
    except ToolError:
        raise
    except Exception as e:
        # An image that cannot be encoded must not lose the caller the capture:
        # the path is still on disk and still readable.
        result["inline"] = f"could not encode for inline viewing ({e}); Read {path}"
        return result
    result[_INLINE_KEY] = {"data": image.pop("data"),
                           "media_type": image["media_type"]}
    result["shown"] = image
    return result


def _parse_region(region: Any) -> tuple[int, int, int, int]:
    if isinstance(region, dict):
        try:
            values = (region["x"], region["y"], region["width"], region["height"])
        except KeyError:
            raise ToolError("region needs x, y, width, height",
                            code="bad_args") from None
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        values = tuple(region)
    else:
        raise ToolError(
            "region must be [x, y, width, height] or an object with those keys",
            code="bad_args")
    x, y, width, height = (int(v) for v in values)
    if width <= 0 or height <= 0:
        raise ToolError(f"refusing to capture a {width}x{height} region",
                        code="bad_args")
    return x, y, width, height


def _screenshot_region(a: dict) -> tuple[tuple[int, int, int, int] | None, dict | None]:
    """The rectangle to capture, and the window it came from if there was one.

    `window` and `region` together used to be refused. They now mean "this
    rectangle, measured inside that window", which is the shape the caller
    actually wanted every time it was refused: a 1200x100 chat composer strip is
    160 tokens against the 1843 of a full screen, and hand-converting it to
    absolute coordinates on every call is how clicks end up 40px off.
    """
    win = None
    if a.get("window") is not None:
        win = _resolve_target(a["window"])
        if win.get("minimized"):
            raise ToolError(
                f'{win["wm_class"]} is minimized, so there is nothing on screen to '
                "capture. Activate it first.",
                code="occluded",
            )

    region = a.get("region")
    if region is None:
        if win is None:
            return None, None
        return (win["x"], win["y"], win["width"], win["height"]), win

    x, y, width, height = _parse_region(region)
    if win is None:
        return (x, y, width, height), None

    # window-relative, clamped to the window so a sloppy strip cannot wander
    # onto whatever is next to it.
    x, y = win["x"] + x, win["y"] + y
    width = min(width, win["x"] + win["width"] - x)
    height = min(height, win["y"] + win["height"] - y)
    if width <= 0 or height <= 0:
        raise ToolError(
            f'region does not fall inside {win["wm_class"]} '
            f'({win["width"]}x{win["height"]}); it is measured from the window\'s '
            "top-left corner when window= is given",
            code="bad_args",
        )
    return (x, y, width, height), win


def _crop(path: Path, region: tuple[int, int, int, int]) -> None:
    Image, _ = _pillow()
    x, y, width, height = region
    with Image.open(path) as img:
        box = (max(0, x), max(0, y), min(img.width, x + width), min(img.height, y + height))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ToolError(f"region {region} does not overlap the screen",
                            code="off_screen")
        img.crop(box).save(path)


def _rescale(path: Path, scale: float) -> None:
    Image, _ = _pillow()
    with Image.open(path) as img:
        img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))
                   ).save(path)


def _label_font(size: int = 13):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        try:
            from PIL import ImageFont
            return ImageFont.load_default()
        except Exception:                                   # pragma: no cover
            return None


def _annotate(path: Path, origin: tuple[int, int], annotate: Any, a: dict,
              scale: float = 1.0) -> dict:
    """Draw the coordinate system onto the picture.

    A model looking at a screenshot has no way to turn "that button" into a
    number, and guessing from proportions is how a click ends up in the wrong
    window. Drawing the grid, the window boxes and the widget boxes -- all
    labelled in SCREEN coordinates, not image ones -- means the number to click
    can be read straight off the image.
    """
    Image, ImageDraw = _pillow()
    options = annotate if isinstance(annotate, dict) else {}
    if annotate is True:
        options = {"grid": True, "windows": True}
    grid = options.get("grid", True)
    spacing = int(grid) if isinstance(grid, int) and not isinstance(grid, bool) else 100
    drawn = {"grid_spacing": spacing if grid else None, "windows": 0, "widgets": 0}

    font = _label_font()

    with Image.open(path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        ox, oy = origin

        def px(sx: float, sy: float) -> tuple[float, float]:
            """Screen coordinates to pixels in this (cropped, scaled) image."""
            return (sx - ox) * scale, (sy - oy) * scale

        def text(sx: float, sy: float, label: str, colour) -> None:
            x, y = px(sx, sy)
            # A dark plate under the label, because pink on a white window and
            # pink on a dark one cannot both be read.
            box = draw.textbbox((x + 2, y + 2), label, font=font)
            draw.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1],
                           fill=(0, 0, 0))
            draw.text((x + 2, y + 2), label, fill=colour, font=font)

        if grid:
            width_s, height_s = img.width / scale, img.height / scale
            first_x = ((ox + spacing - 1) // spacing) * spacing
            for sx in range(int(first_x), int(ox + width_s), spacing):
                x, _ = px(sx, 0)
                draw.line([(x, 0), (x, img.height)], fill=(255, 0, 128), width=1)
                text(sx, oy, str(sx), (255, 120, 190))
            first_y = ((oy + spacing - 1) // spacing) * spacing
            for sy in range(int(first_y), int(oy + height_s), spacing):
                _, y = px(0, sy)
                draw.line([(0, y), (img.width, y)], fill=(255, 0, 128), width=1)
                text(ox, sy, str(sy), (255, 120, 190))

        if options.get("windows", True):
            for win in reversed(list_windows()):
                if win.get("minimized"):
                    continue
                x0, y0 = px(win["x"], win["y"])
                x1, y1 = px(win["x"] + win["width"], win["y"] + win["height"])
                draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 200, 255), width=2)
                text(win["x"], win["y"],
                     f'{win["id"]} {win["wm_class"]} @{win["x"]},{win["y"]}',
                     (0, 200, 255))
                drawn["windows"] += 1

        if options.get("widgets"):
            from .atspi import (  # late: atspi imports this module
                _atspi_app_for_window,
                _clickable_widgets,
            )
            app = a.get("app")
            if not app:
                focused = [w for w in list_windows() if w.get("focused")]
                app = _atspi_app_for_window(focused[0]) if focused else None
            if app:
                for widget in _clickable_widgets(app, int(options.get("limit") or 40)):
                    b = widget["bounds"]
                    x0, y0 = px(b["x"], b["y"])
                    x1, y1 = px(b["x"] + b["w"], b["y"] + b["h"])
                    draw.rectangle([x0, y0, x1, y1], outline=(0, 255, 120), width=1)
                    text(b["x"], b["y"],
                         f'{widget["click_at"][0]},{widget["click_at"][1]} '
                         f'{widget["name"][:24]}', (0, 255, 120))
                    drawn["widgets"] += 1
                drawn["widgets_app"] = app
        img.save(path)
    return drawn


def tool_screencast(a: dict) -> dict:
    # Deliberately a subprocess rather than an import: screencast.py has to hold
    # the D-Bus connection open for the whole recording, because Mutter destroys
    # the session the instant that connection drops. Running it in-process would
    # block this server's stdio loop for the entire duration.
    path = Path(os.path.expanduser(str(a.get("path") or ""))).absolute()
    if path.is_dir():
        raise ToolError(f"{path} is a directory; give a file path ending in .mp4",
                        code="bad_args")
    if path.suffix.lower() != ".mp4":
        raise ToolError(f"path must end in .mp4, got {path.name!r}", code="bad_args")
    path.parent.mkdir(parents=True, exist_ok=True)

    seconds = float(a.get("seconds", 10))
    if not 0.5 <= seconds <= 120:
        raise ToolError(f"seconds must be between 0.5 and 120, got {seconds}",
                        code="bad_args")

    cmd = [sys.executable, str(HERE / "screencast.py"),
           "--seconds", str(seconds), "--fps", str(int(a.get("fps", 30)))]
    if a.get("target") is not None:
        cmd += ["--window", str(_resolve_target(a["target"])["id"])]
    if a.get("include_cursor"):
        cmd.append("--cursor")
    cmd.append(str(path))

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=seconds + 60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise ToolError(f"recording failed: {detail[:400]}", code="capture_failed")
    result = json.loads(proc.stdout)
    # An mp4 is opaque to a model, so a bare path is a dead end -- the caller
    # would either try to Read the video or fall back to guessing. Name the
    # exact next call here, at the one moment it is certain to be read.
    result["next_step"] = (
        f'The mp4 cannot be read directly. Call frames{{"path": "{path}"}} to '
        "tile its frames into one PNG, then Read that PNG."
    )
    return result


def _frames_once(a: dict, raw: Any) -> dict:
    path = Path(os.path.expanduser(str(raw or ""))).absolute()
    if not path.exists():
        raise ToolError(f"{path} does not exist", code="bad_args")
    if path.suffix.lower() not in (".mp4", ".mkv", ".webm", ".mov"):
        raise ToolError(f"expected a video file, got {path.name!r}", code="bad_args")

    outdir = a.get("outdir")
    outdir = (Path(os.path.expanduser(str(outdir))).absolute() if outdir
              else path.parent / f"{path.stem}-frames")

    cols, rows = int(a.get("cols", 4)), int(a.get("rows", 3))
    if not (1 <= cols <= 8 and 1 <= rows <= 8):
        raise ToolError(f"cols and rows must each be 1-8, got {cols}x{rows}",
                        code="bad_args")

    cmd = [sys.executable, str(HERE / "frames.py"),
           str(path), str(outdir), "--cols", str(cols), "--rows", str(rows),
           "--json"]
    for flag, key in (("--from-frame", "from_frame"), ("--to-frame", "to_frame")):
        if a.get(key) is not None:
            cmd += [flag, str(int(a[key]))]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise ToolError("frame extraction failed: "
                        f"{(proc.stderr or proc.stdout or '').strip()[:400]}",
                        code="capture_failed")
    return json.loads(proc.stdout)


def tool_frames(a: dict) -> dict:
    """Contact sheet + motion measurement for a recording. See frames.py.

    The sheet comes back inline for the same reason screenshots do: it existed
    to be looked at, and returning a path meant a second round trip to Read it.
    `compare` stacks a second recording underneath the first, which is what a
    before/after actually needs -- two calls and two Reads used to mean judging
    two images that were never on screen together.
    """
    result = _frames_once(a, a.get("path"))
    sheet = result.get("contact_sheet") or result.get("sheet")

    other = a.get("compare")
    if other:
        second = _frames_once(a, other)
        result = {"first": result, "second": second}
        sheet2 = second.get("contact_sheet") or second.get("sheet")
        if sheet and sheet2:
            try:
                sheet = _stack_sheets(Path(sheet), Path(sheet2))
                result["compared_sheet"] = str(sheet)
            except Exception as e:                          # pragma: no cover
                result["compare_note"] = f"could not stack the two sheets: {e}"

    if sheet and a.get("inline", True):
        _attach_inline(result, Path(sheet), a)
    return result


def _stack_sheets(top: Path, bottom: Path) -> Path:
    """Two contact sheets in one image, first above second, same width."""
    Image, ImageDraw = _pillow()
    with Image.open(top) as a_img, Image.open(bottom) as b_img:
        a_rgb, b_rgb = a_img.convert("RGB"), b_img.convert("RGB")
        width = max(a_rgb.width, b_rgb.width)
        if b_rgb.width != width:
            b_rgb = b_rgb.resize((width, int(b_rgb.height * width / b_rgb.width)),
                                 Image.LANCZOS)
        if a_rgb.width != width:
            a_rgb = a_rgb.resize((width, int(a_rgb.height * width / a_rgb.width)),
                                 Image.LANCZOS)
        gap = 28
        out = Image.new("RGB", (width, a_rgb.height + b_rgb.height + gap * 2), "black")
        out.paste(a_rgb, (0, gap))
        out.paste(b_rgb, (0, a_rgb.height + gap * 2))
        draw = ImageDraw.Draw(out)
        font = _label_font(18)
        draw.text((8, 4), "FIRST", fill="white", font=font)
        draw.text((8, a_rgb.height + gap + 4), "SECOND", fill="white", font=font)
        target = top.with_name(f"{top.parent.name}-vs-{bottom.parent.name}.png")
        out.save(target)
    return target


def tool_region_changed(a: dict) -> dict:
    """Wait until part of the screen changes, without spending a picture a poll.

    `wait_for` watches windows appearing and focus moving; it has nothing to say
    about a message arriving in a chat or a spinner finishing. That was polled
    with blind screenshots -- three full captures plus three image Reads for one
    15-second reply -- because there was no other way to ask.
    """
    timeout = float(a.get("timeout") or 10)
    if not 0.2 <= timeout <= 300:
        raise ToolError("timeout must be between 0.2 and 300 seconds",
                        code="bad_args")
    poll = max(0.1, float(a.get("poll_seconds") or 0.3))

    region, window = _screenshot_region(a)
    path, _ = _shot_path({})
    # A waiting tool can afford a proper look at what is already moving: four
    # frames over about half a second, and anything that toggles between them is
    # a caret or a spinner rather than the event being waited for.
    baseline = _baseline(path, region, gap=max(0.12, min(poll, 0.2)), frames=4)
    ambient = _ambient_cells(baseline)

    start = time.monotonic()
    polls, best = 1, {"percent": 0.0, "strong_cells": 0, "max_delta": 0}
    while True:
        waited = time.monotonic() - start
        if waited >= timeout:
            return {"changed": False, "waited_seconds": round(waited, 2),
                    "polls": polls, "largest_change_seen": best,
                    "watched": (window["wm_class"] if window else
                                list(region) if region else "whole screen"),
                    "moving_on_its_own": len(ambient),
                    "detail": "nothing changed before the timeout; nothing was altered "
                              "by waiting"}
        time.sleep(poll)
        _capture(path, region)
        polls += 1
        delta = _changed_since(baseline, _fingerprint(path), ambient)
        landed = delta.pop("landed")
        if delta["strong_cells"] > best["strong_cells"]:
            best = delta
        if landed:
            result = {"changed": True, "waited_seconds": round(time.monotonic() - start, 2),
                      "polls": polls, "change": delta,
                      "watched": (window["wm_class"] if window else
                                  list(region) if region else "whole screen")}
            if a.get("look", True) is not False:
                _attach_inline(result, path, a)
                shown = result.get("shown")
                if isinstance(shown, dict):
                    origin = (region[0], region[1]) if region else (0, 0)
                    result["coordinate_note"] = _coordinate_note(
                        shown["dimensions"], _png_dimensions(path), origin)
            return result
