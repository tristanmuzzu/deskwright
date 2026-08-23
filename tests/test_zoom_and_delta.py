#!/usr/bin/env python3
"""In-process proof of `zoom` and the "what changed" summary. NO desktop.

Captures are faked with Pillow-drawn PNGs and fingerprints are synthesised, so
this runs on any machine with Pillow -- no gnome-shell, no D-Bus, no windows.
The OCR checks degrade honestly when tesseract is absent: the code under test
must SAY it skipped, and that is what gets asserted.

    ./tests/test_zoom_and_delta.py
"""
from __future__ import annotations

import base64
import io
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from PIL import Image, ImageDraw, ImageFont

from wcu import capture
from wcu.errors import ToolError
from wcu.ocr import ocr_snippet

CHECKS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="wcu-zoom-test-", dir="/tmp"))

# The synthetic "screen": 2000x1200, with every pixel's colour encoding its own
# coordinates, so crop arithmetic is verifiable from the pixels themselves even
# after a lossy JPEG round trip.
SCREEN_W, SCREEN_H = 2000, 1200


def check(label: str, ok: bool, detail: str) -> None:
    CHECKS.append((label, ok, detail))


def build_screen() -> Image.Image:
    img = Image.new("RGB", (SCREEN_W, SCREEN_H))
    px = img.load()
    for y in range(0, SCREEN_H, 4):                    # 4px blocks: fast enough
        for x in range(0, SCREEN_W, 4):
            colour = (x * 255 // SCREEN_W, y * 255 // SCREEN_H, 128)
            for dy in range(4):
                for dx in range(4):
                    px[x + dx, y + dy] = colour
    return img


SCREEN = build_screen()


def fake_capture(path: Path, region=None, include_cursor: bool = False) -> bool:
    img = SCREEN
    if region:
        x, y, w, h = (int(v) for v in region)
        img = SCREEN.crop((x, y, x + w, y + h))
    img.save(path)
    return True


def expect_bad_args(label: str, args: dict, needle: str = "") -> None:
    try:
        capture.tool_zoom(args)
        check(label, False, "no error raised")
    except ToolError as e:
        check(label, e.code == "bad_args" and needle.lower() in str(e).lower(),
              f"code={e.code} msg={str(e)[:90]}")


def shown_image(result: dict) -> Image.Image:
    raw = base64.b64decode(result[capture._INLINE_KEY]["data"])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def test_zoom() -> None:
    real = (capture._capture, capture._desktop_size, capture._resolve_target,
            capture._occlusion)
    capture._capture = fake_capture
    capture._desktop_size = lambda: (SCREEN_W, SCREEN_H)
    try:
        # -- validation ---------------------------------------------------
        expect_bad_args("zoom with no target is refused", {}, "region")
        expect_bad_args("zoom refuses scale",
                        {"region": [0, 0, 50, 50], "scale": 2}, "scale")
        expect_bad_args("zoom refuses a negative pad",
                        {"region": [0, 0, 50, 50], "pad": -4}, "pad")
        expect_bad_args("zoom refuses a zero-size region",
                        {"region": [10, 10, 0, 40]}, "region")
        # 1500x900 = 1.35M px against a 2.4M px desktop: over half.
        expect_bad_args("a region over half the desktop is refused",
                        {"region": [0, 0, 1500, 900]}, "screenshot")
        # 1400x850 = 1.19M px: just under half. Pad pushes it over.
        expect_bad_args("pad counts against the half-desktop limit",
                        {"region": [0, 0, 1400, 850], "pad": 40}, "screenshot")

        # -- crop arithmetic, from the pixels themselves ------------------
        out = TMP / "zoom-a.png"
        r = capture.tool_zoom({"region": [100, 50, 200, 100], "pad": 10,
                               "path": str(out)})
        check("pad expands the region on all four sides",
              r["region"] == {"x": 90, "y": 40, "width": 220, "height": 120},
              f'region={r["region"]}')
        check("the file on disk is exactly the region",
              r["dimensions"] == "220x120", f'dimensions={r["dimensions"]}')
        shown = r.get("shown") or {}
        check("the shown image is not scaled",
              shown.get("dimensions") == "220x120",
              f'shown={shown.get("dimensions")}')
        img = shown_image(r)
        want = (90 * 255 // SCREEN_W, 40 * 255 // SCREEN_H, 128)
        got = img.getpixel((2, 2))
        close = all(abs(a - b) <= 16 for a, b in zip(got, want))
        check("pixel (0,0) of the image is screen (90,40)",
              close, f"want~{want} got={got}")
        check("the coordinate note maps image pixels to screen",
              "90 + px" in (r.get("coordinate_note") or "")
              and "40 + py" in (r.get("coordinate_note") or ""),
              f'note={r.get("coordinate_note")}')

        # -- never scaled, even past the model's 1568px ceiling -----------
        big = capture.tool_zoom({"region": [0, 0, 1800, 600],
                                 "path": str(TMP / "zoom-b.png")})
        check("a permitted zoom wider than 1568px stays 1:1",
              (big.get("shown") or {}).get("dimensions") == "1800x600",
              f'shown={(big.get("shown") or {}).get("dimensions")}')

        # -- window target, resolved like tool_screenshot -----------------
        capture._resolve_target = lambda t: {
            "id": 7, "wm_class": "fake-app", "title": "Fake", "x": 300,
            "y": 200, "width": 150, "height": 100, "minimized": False}
        capture._occlusion = lambda w: None
        w = capture.tool_zoom({"window": "fake-app", "pad": 5,
                               "path": str(TMP / "zoom-c.png")})
        check("window resolves to its rectangle plus pad",
              w["region"] == {"x": 295, "y": 195, "width": 160, "height": 110}
              and w["dimensions"] == "160x110",
              f'region={w["region"]} dims={w["dimensions"]}')

        # -- unknown desktop size: proceed rather than refuse on a guess --
        capture._desktop_size = lambda: None
        u = capture.tool_zoom({"region": [0, 0, 1500, 900],
                               "path": str(TMP / "zoom-d.png")})
        check("an unmeasurable desktop skips the size check",
              u["dimensions"] == "1500x900", f'dims={u["dimensions"]}')
    finally:
        (capture._capture, capture._desktop_size, capture._resolve_target,
         capture._occlusion) = real


# =========================================================================
# what changed: geometry through _frame_delta and _delta_where
# =========================================================================
GW, GH = capture.FINGERPRINT


def fp(cells: dict[int, int]) -> bytes:
    buf = bytearray(GW * GH)
    for i, v in cells.items():
        buf[i] = v
    return bytes(buf)


def idx(cx: int, cy: int) -> int:
    return cy * GW + cx


def test_delta_where() -> None:
    before = fp({})
    block = {idx(x, y): 200 for x in range(10, 20) for y in range(5, 10)}
    after = fp(block)
    rect = (0, 0, 1920, 1080)                          # cells are 8x8 px here
    windows = [                                        # bottom of stack first
        {"id": 1, "wm_class": "left-app", "x": 0, "y": 0,
         "width": 200, "height": 1080},
        {"id": 2, "wm_class": "right-app", "x": 200, "y": 0,
         "width": 1720, "height": 1080},
    ]

    check("_frame_delta agrees the block is 50 strong cells",
          capture._frame_delta(before, after)["strong_cells"] == 50,
          f'delta={capture._frame_delta(before, after)}')

    where = capture._delta_where([before, before], after, set(), rect, windows)
    check("the bounding box lands where the cells changed",
          where is not None
          and where["box"] == {"x": 80, "y": 40, "width": 80, "height": 40},
          f'where={where}')
    check("cell count is the changed area, not the box",
          where is not None and where["cells"] == 50, f'where={where}')
    check("only the intersecting window is named",
          where is not None and where["windows"] == ["left-app"],
          f'windows={where and where["windows"]}')
    check("the detail says where in words",
          where is not None and "80x40" in where["detail"]
          and "left-app" in where["detail"], f'detail={where and where["detail"]}')

    # A capture rect with an origin: same cells, translated coordinates.
    shifted = capture._delta_where([before], after, None, (500, 300, 1920, 1080))
    check("the box is translated by the capture origin",
          shifted is not None and shifted["box"] == {"x": 580, "y": 340,
                                                     "width": 80, "height": 40},
          f'shifted={shifted}')
    check("without a window list, no claim about windows is made",
          shifted is not None and "windows" not in shifted,
          f'keys={shifted and sorted(shifted)}')

    # An ambient cell (a caret, a clock) must not stretch the box.
    caret = idx(100, 100)
    noisy = fp({**block, caret: 200})
    masked = capture._delta_where([before], noisy, {caret}, rect, windows)
    unmasked = capture._delta_where([before], noisy, set(), rect, windows)
    check("ambient cells are excluded from the box",
          masked is not None
          and masked["box"] == {"x": 80, "y": 40, "width": 80, "height": 40},
          f'masked={masked}')
    check("...and without the mask the same cell stretches it",
          unmasked is not None and unmasked["box"]["height"] > 40,
          f'unmasked={unmasked}')

    # No change at all: nothing to say.
    check("an unchanged frame yields no location",
          capture._delta_where([before], before, set(), rect, windows) is None,
          "expected None")

    # A large low-contrast change (a scroll): no strong cells, so the box
    # falls back to the noise-threshold cells.
    weak = fp({idx(x, y): 30 for x in range(100) for y in range(50)})
    soft = capture._delta_where([before], weak, set(), rect, windows)
    check("a low-contrast change still gets a box, from the noise cells",
          soft is not None
          and soft["box"] == {"x": 0, "y": 0, "width": 800, "height": 400},
          f'soft={soft}')

    # The box spans x 80..160; a window starting exactly at 160 only abuts it.
    edge = capture._delta_where(
        [before], after, set(), rect,
        [{"id": 3, "wm_class": "beside", "x": 160, "y": 40,
          "width": 100, "height": 100}])
    check("a window that only abuts the box does not count",
          edge is not None and edge["windows"] == [],
          f'edge={edge}')


def test_changed_text() -> None:
    # A PNG a change summary would crop: white, with clear dark text.
    img = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    draw.text((60, 100), "HELLO 42", fill="black", font=font)
    path = TMP / "changed.png"
    img.save(path)
    rect = (0, 0, 800, 600)
    box = {"x": 50, "y": 90, "width": 250, "height": 60}

    have_tesseract = bool(shutil.which("tesseract"))
    result = capture._changed_text(path, box, rect)
    if have_tesseract:
        check("a small changed area is read back as words",
              result is not None and "HELLO" in (result.get("text") or ""),
              f'result={result}')
        check("the OCR stayed inside its budget",
              result is not None
              and (result.get("seconds") or 0) <= capture.CHANGE_OCR_BUDGET_S,
              f'result={result}')
    else:
        check("a missing tesseract is said, not raised",
              result is not None and "tesseract" in (result.get("skipped") or ""),
              f'result={result}')

    # Too big in pixels: the image is the answer, so no OCR is attempted.
    check("a change too big in pixels skips OCR entirely",
          capture._changed_text(path, {"x": 0, "y": 0, "width": 800,
                                       "height": 600}, rect) is None,
          "expected None")
    # Too big relative to the frame: more than half the fingerprint's cells.
    small_rect = (0, 0, 400, 300)
    check("a change covering most of a small frame skips OCR",
          capture._changed_text(path, {"x": 20, "y": 20, "width": 350,
                                       "height": 250}, small_rect) is None,
          "expected None")

    # The budget is hard: 0 seconds means an honest skip, not a stall.
    zero = ocr_snippet(path, (0, 0, 400, 200), budget_s=0.0)
    check("a spent budget is reported as skipped",
          "budget" in (zero.get("skipped") or ""), f'zero={zero}')
    outside = ocr_snippet(path, (900, 700, 1000, 800))
    check("a crop outside the image is reported as skipped",
          "outside" in (outside.get("skipped") or ""), f'outside={outside}')


def test_look_integration() -> None:
    """The full _look path, with the desktop faked out from under it."""
    before_img = Image.new("RGB", (800, 600), "white")
    after_img = before_img.copy()
    draw = ImageDraw.Draw(after_img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    draw.text((50, 100), "HELLO 42", fill="black", font=font)

    before_path, after_path = TMP / "look-before.png", TMP / "look-after.png"
    before_img.save(before_path)
    after_img.save(after_path)
    before_fp = capture._fingerprint(before_path)
    after_fp = capture._fingerprint(after_path)
    region = (0, 0, 800, 600)
    fake_windows = [{"id": 9, "wm_class": "the-app", "x": 0, "y": 0,
                     "width": 800, "height": 600, "minimized": False}]

    real = (capture._shot_path, capture._settle, capture.list_windows)
    capture._shot_path = lambda a: (after_path, True)
    capture._settle = lambda path, region, max_wait=1.5: {
        "settled": True, "frames": 4, "noise_percent": 0.0,
        "waited_seconds": 0.1, "fingerprint": after_fp, "ambient": set()}
    capture.list_windows = lambda: fake_windows
    try:
        prepared = capture._Look("region", region, None, [before_fp, before_fp])
        result = capture._look({"look": "region", "look_at": {
            "x": 0, "y": 0, "width": 800, "height": 600}}, {}, prepared)
    finally:
        capture._shot_path, capture._settle, capture.list_windows = real

    view = result.get("look") or {}
    check("the verdict still reads as a hit",
          "landed" in (view.get("verdict") or ""), f'verdict={view.get("verdict")}')
    check("existing fields are intact next to the new ones",
          all(k in view for k in ("of", "settled", "changed", "verdict")),
          f'keys={sorted(view)}')
    where = view.get("changed_where") or {}
    box = where.get("box") or {}
    # The text was drawn at (50,100); the ink itself starts a few px lower
    # (font ascent) and strong cells shave low-ink edges, so the ranges allow
    # a couple of 3.3x4.4px cells of slack on every side.
    plausible = (box and 40 <= box["x"] <= 60 and 95 <= box["y"] <= 115
                 and 120 <= box["width"] <= 220 and 15 <= box["height"] <= 60)
    check("changed_where boxes the drawn text", bool(plausible),
          f'box={box} detail={where.get("detail")}')
    check("the box is attributed to the faked window",
          where.get("windows") == ["the-app"], f'windows={where.get("windows")}')
    text = view.get("changed_text") or {}
    if shutil.which("tesseract"):
        check("changed_text reads the new words",
              "HELLO" in (text.get("text") or "") or "skipped" in text,
              f'text={text}')
    check("the image is still attached on a hit",
          capture._INLINE_KEY in result, f'keys={sorted(result)}')


def main() -> int:
    test_zoom()
    test_delta_where()
    test_changed_text()
    test_look_integration()

    width = max(len(c[0]) for c in CHECKS)
    for label, ok, detail in CHECKS:
        print(f'{"PASS" if ok else "FAIL"}  {label:<{width}}  {detail}')
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
