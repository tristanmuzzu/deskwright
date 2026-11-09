#!/usr/bin/env python3
"""Typing that AT-SPI cannot read is checked by OCR; own popups are not "occluded".

No desktop. The OCR test needs tesseract and draws its own text with Pillow;
it is skipped where tesseract is missing.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import capture, ocr, shell
from deskwright import input as inp


def _text_png(tmp_path, text):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 120), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((20, 40), text, fill="black", font=font)
    path = tmp_path / "typed.png"
    img.save(path)
    return path


@pytest.mark.skipif(not shutil.which("tesseract"), reason="needs tesseract")
def test_ocr_finds_typed_text_and_does_not_echo_it(tmp_path):
    path = _text_png(tmp_path, "Meeting moved to Thursday 3pm")
    seen = ocr.ocr_contains(path, None, "meeting moved to thursday 3pm")
    assert seen["checked"] and seen["found"], seen
    assert set(seen) <= {"checked", "found", "similarity", "seconds"}   # no screen text


@pytest.mark.skipif(not shutil.which("tesseract"), reason="needs tesseract")
def test_ocr_does_not_find_text_that_is_not_there(tmp_path):
    path = _text_png(tmp_path, "Something else entirely")
    seen = ocr.ocr_contains(path, None, "correct horse battery staple")
    assert seen["checked"] and not seen["found"]


def test_ocr_refuses_to_judge_very_short_text(tmp_path):
    assert ocr.ocr_contains(tmp_path / "x.png", None, "ok")["checked"] is False


def test_unreadable_typing_is_verified_by_ocr(monkeypatch, tmp_path):
    monkeypatch.setattr("deskwright.ocr.ocr_contains",
                        lambda path, crop, text: {"checked": True, "found": True,
                                                  "similarity": 0.97})
    monkeypatch.setattr(capture, "_png_dimensions", lambda p: "200x100")
    result = {"verified": False, "detail": "sent 5 characters"}
    view = {"path": str(tmp_path / "a.png"),
            "changed_where": {"box": {"x": 10, "y": 10, "width": 50, "height": 20}}}
    capture._ocr_verify_typed({"text": "hello"}, result, view,
                              capture._Look("window", (0, 0, 200, 100), None, None))
    assert result["verified"] is True and result["verification"] == "ocr"
    assert result["ocr_check"]["area"] == "changed area"


def test_unfound_typing_says_why_it_might_be_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("deskwright.ocr.ocr_contains",
                        lambda path, crop, text: {"checked": True, "found": False,
                                                  "similarity": 0.3})
    monkeypatch.setattr(capture, "_png_dimensions", lambda p: "200x100")
    result = {"verified": False, "detail": "sent"}
    capture._ocr_verify_typed({"text": "hunter2!!"}, result, {"path": str(tmp_path / "a")},
                              capture._Look("window", (0, 0, 200, 100), None, None))
    assert result["verified"] is False and result["verification"] == "ocr_not_found"
    assert "password" in result["detail"]


WANTED = {"id": 1, "pid": 500, "wm_class": "Audition", "title": "Main"}


@pytest.mark.parametrize("popup, accepted", [
    ({"id": 2, "pid": 500, "type": "DROPDOWN_MENU", "transient_for": None}, True),
    ({"id": 2, "pid": 999, "type": "NORMAL", "transient_for": 1}, True),
    ({"id": 2, "pid": 999, "type": "POPUP_MENU", "transient_for": None}, False),
    ({"id": 2, "pid": 500, "type": "NORMAL", "transient_for": None}, False),
])
def test_click_into_the_expected_windows_own_popup(monkeypatch, popup, accepted):
    monkeypatch.setattr(shell, "list_windows", lambda: [WANTED, popup])
    got = inp._owned_popup({"id": 2}, WANTED)
    assert bool(got) is accepted
    if accepted:
        assert "Audition" in got


def test_guard_accepts_an_owned_popup_and_says_so(monkeypatch):
    popup = {"id": 2, "pid": 500, "wm_class": None, "title": "", "type": "POPUP_MENU",
             "transient_for": 1}
    monkeypatch.setattr(inp, "_resolve_target", lambda e: WANTED)
    monkeypatch.setattr(inp, "window_at", lambda x, y: {"window": popup, "covering": [popup],
                                                        "source": "compositor pick"})
    monkeypatch.setattr(shell, "list_windows", lambda: [WANTED, popup])
    out = inp._guard_point(5, 5, 1)
    assert "popup menu of Audition" in out["received_by"]
