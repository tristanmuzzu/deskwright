from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .capture import _capture, _pillow, _screenshot_region, _shot_path
from .errors import ToolError

# =========================================================================
# reading the screen without spending a picture on it
# =========================================================================

OCR_MIN_CONFIDENCE = 55
OCR_PSM_REGION = 6          # "one uniform block" -- a window or a toolbar
OCR_PSM_SCREEN = 11         # "sparse text" -- a whole desktop of separate widgets
OCR_UPSCALE_UNDER = 700     # px on the long edge; below this, OCR needs help


def tool_find_text(a: dict) -> dict:
    """Where a piece of visible text is, in screen coordinates.

    AT-SPI is the right answer and is tried first everywhere else in this file,
    but Chrome exposes three actionable nodes for an entire browser, Electron
    two, and Telegram's Qt tree is unreachable behind AppArmor. For those, the
    only thing that knows where the "Send" button is, is the picture -- and
    finding it currently means a screenshot, an image Read, and arithmetic on a
    scaled crop, which is 14 seconds and has put clicks 40px off.

    This is that loop, done here: about 1.5s and no image in the transcript.
    """
    needle = str(a.get("text") or "").strip()
    if not needle:
        raise ToolError("text is required: the visible string to look for",
                        code="bad_args")
    if not shutil.which("tesseract"):
        raise ToolError("this needs tesseract (tesseract-ocr) and it is not installed",
                        code="capture_failed")

    region, window = _screenshot_region(a)
    path, _ = _shot_path({})
    _capture(path, region)
    origin = (region[0], region[1]) if region else (0, 0)

    # Measured 2026-08-22: on a 360x616 window psm 6 reads 35 words against psm
    # 11's 16; on the whole 1920x1080 desktop psm 6 reads 290 and MISSES the
    # calculator entirely while psm 11 reads 309 and finds it. One block of text
    # is what a window is and what a desktop is not.
    default_psm = OCR_PSM_REGION if region else OCR_PSM_SCREEN
    words, scale = _ocr_words(path, int(a.get("min_confidence") or OCR_MIN_CONFIDENCE),
                              int(a.get("psm") or default_psm))
    matches = _match_phrase(words, needle, bool(a.get("exact")))

    results = []
    for m in matches:
        x = origin[0] + m["x"] / scale
        y = origin[1] + m["y"] / scale
        w, h = m["width"] / scale, m["height"] / scale
        results.append({
            "text": m["text"],
            "click_at": [round(x + w / 2), round(y + h / 2)],
            "bounds": {"x": round(x), "y": round(y),
                       "width": round(w), "height": round(h)},
            "confidence": m["confidence"],
        })
    results.sort(key=lambda r: (-r["confidence"], r["bounds"]["y"]))
    limit = int(a.get("limit") or 10)

    out: dict[str, Any] = {
        "query": needle,
        "matches": len(results),
        "results": results[:limit],
        "searched": ({"window": window["wm_class"]} if window else
                     {"region": list(region)} if region else {"screen": True}),
        "words_read": len(words),
    }
    if not results:
        out["detail"] = (
            f"{len(words)} words were read and none matched {needle!r}. OCR misses "
            "icon-only buttons and low-contrast text entirely; try ui_find if the "
            "app has an accessibility tree, or take a picture and look."
        )
    # Additive tripwire over everything that was read, matches and context
    # alike; a tripwire failure must never break the find.
    try:
        from .tripwire import check as _injection_check
        warning = _injection_check(" ".join(w["text"] for w in words))
    except Exception:
        warning = None
    if warning:
        out["injection_warning"] = warning
    return out


def _ocr_words(path: Path, min_confidence: int,
               psm: int = OCR_PSM_REGION) -> tuple[list[dict], float]:
    """Every legible word with its box, plus the factor its coordinates are in.

    Small captures are upscaled first: tesseract wants roughly 300dpi text and
    UI text at 1:1 is closer to 96, so a 360px-wide dialog reads as noise until
    it is enlarged. Measured on this machine: a full 1920x1080 screen is 3.7s,
    half of one is 1.6s.
    """
    Image, _ = _pillow()
    scale = 1.0
    source = path
    with Image.open(path) as img:
        if max(img.size) < OCR_UPSCALE_UNDER:
            scale = 2.0
            source = path.with_name(f".{path.name}.ocr.png")
            img.convert("RGB").resize((int(img.width * scale), int(img.height * scale)),
                                      Image.LANCZOS).save(source)
    try:
        # psm 6 -- "one uniform block of text" -- rather than the default 3.
        # Measured on a calculator window: psm 3 reads 9 words, psm 6 reads 35.
        # Page segmentation is looking for paragraphs and columns, and a toolbar
        # is neither, so it discards most of the interface as non-text.
        proc = subprocess.run(["tesseract", str(source), "stdout",
                               "--psm", str(psm), "tsv"],
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ToolError("tesseract did not finish within 60s",
                        code="capture_failed") from None
    finally:
        if source is not path:
            source.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise ToolError(f"tesseract failed: {(proc.stderr or '').strip()[:300]}",
                        code="capture_failed")
    return _parse_tsv(proc.stdout, min_confidence), scale


def _parse_tsv(stdout: str, min_confidence: int) -> list[dict]:
    """tesseract's TSV, as word dicts, in the reading order tesseract emits."""
    words = []
    for line in stdout.splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) < 12 or not cells[11].strip():
            continue
        try:
            confidence = float(cells[10])
        except ValueError:
            continue
        if confidence < min_confidence:
            continue
        words.append({"text": cells[11], "confidence": round(confidence),
                      "line": tuple(cells[1:5]),
                      "x": int(cells[6]), "y": int(cells[7]),
                      "width": int(cells[8]), "height": int(cells[9])})
    return words


OCR_SNIPPET_BUDGET_S = 0.5
OCR_SNIPPET_MAX_WORDS = 10


def ocr_snippet(path: Path, crop: tuple[int, int, int, int],
                budget_s: float = OCR_SNIPPET_BUDGET_S,
                max_words: int = OCR_SNIPPET_MAX_WORDS) -> dict:
    """A few words of what one small area of `path` shows, inside a hard budget.

    Built for capture.py's "what changed" summary: the area is small by
    contract, the answer is a phrase rather than a layout, and the whole thing
    is a bonus -- a bonus that doubles the latency of every click is a
    regression, so the budget is enforced with time.monotonic and tesseract is
    given only what remains of it. Never raises for a missing tesseract or a
    blown budget; it says so instead.

    `crop` is (left, top, right, bottom) in the image's own pixels.
    """
    start = time.monotonic()
    if not shutil.which("tesseract"):
        return {"skipped": "tesseract is not installed"}

    Image, _ = _pillow()
    source = path.with_name(f".{path.name}.snip.png")
    try:
        with Image.open(path) as img:
            box = (max(0, int(crop[0])), max(0, int(crop[1])),
                   min(img.width, int(crop[2])), min(img.height, int(crop[3])))
            if box[2] <= box[0] or box[3] <= box[1]:
                return {"skipped": "the changed area falls outside the image"}
            snip = img.convert("RGB").crop(box)
            if max(snip.size) < OCR_UPSCALE_UNDER:
                snip = snip.resize((snip.width * 2, snip.height * 2),
                                   Image.LANCZOS)
            snip.save(source)

        remaining = budget_s - (time.monotonic() - start)
        if remaining <= 0.05:
            return {"skipped": f"the {budget_s}s OCR budget was spent before "
                               "tesseract could run"}
        try:
            proc = subprocess.run(["tesseract", str(source), "stdout",
                                   "--psm", str(OCR_PSM_REGION), "tsv"],
                                  capture_output=True, text=True,
                                  timeout=remaining, check=False)
        except subprocess.TimeoutExpired:
            return {"skipped": f"tesseract blew the {budget_s}s OCR budget; "
                               "the attached image is the authority"}
        if proc.returncode != 0:
            return {"skipped": "tesseract failed: "
                               f"{(proc.stderr or '').strip()[:120]}"}
    finally:
        source.unlink(missing_ok=True)

    elapsed = round(time.monotonic() - start, 2)
    if elapsed > budget_s:
        return {"skipped": f"OCR took {elapsed}s, over the {budget_s}s budget"}
    words = _parse_tsv(proc.stdout, OCR_MIN_CONFIDENCE)
    texts = [w["text"] for w in words[:max_words]]
    if not texts:
        return {"text": "", "note": "no legible text in the changed area",
                "seconds": elapsed}
    out: dict[str, Any] = {"text": " ".join(texts), "seconds": elapsed}
    if len(words) > max_words:
        out["note"] = f"first {max_words} of {len(words)} words"
    return out


def _match_phrase(words: list[dict], needle: str, exact: bool) -> list[dict]:
    """Single words, and runs of consecutive words on one line for phrases."""
    wanted = needle if exact else needle.lower()

    def norm(s: str) -> str:
        return s if exact else s.lower()

    hits = []
    for word in words:
        text = norm(word["text"])
        if text == wanted or (not exact and wanted in text):
            hits.append(dict(word))

    if " " in needle:
        parts = wanted.split()
        for i in range(len(words) - len(parts) + 1):
            run = words[i:i + len(parts)]
            if any(w["line"] != run[0]["line"] for w in run):
                continue
            if [norm(w["text"]) for w in run] != parts:
                continue
            left = min(w["x"] for w in run)
            top = min(w["y"] for w in run)
            hits.append({
                "text": " ".join(w["text"] for w in run),
                "confidence": round(sum(w["confidence"] for w in run) / len(run)),
                "x": left, "y": top,
                "width": max(w["x"] + w["width"] for w in run) - left,
                "height": max(w["y"] + w["height"] for w in run) - top,
            })
    return hits
