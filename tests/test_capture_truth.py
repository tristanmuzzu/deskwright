#!/usr/bin/env python3
"""Captures say when they might be lying, and zoom shows the same instant.

No desktop: captures are Pillow images, the display power state and the
window list are patched. See the comments on display_power, _repeat_note,
_zoom_observation and _no_change_hints in deskwright/capture.py.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
PIL = pytest.importorskip("PIL")
from PIL import Image

from deskwright import capture, observations, server
from deskwright.errors import ToolError

SCREEN = Image.new("RGB", (400, 300), (10, 20, 30))
for i in range(0, 400, 20):
    SCREEN.paste((i % 255, 100, 200), (i, 0, i + 10, 300))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = {"power": 0, "screen": SCREEN.copy()}

    def cap(path, region=None, include_cursor=False):
        img = state["screen"]
        if region:
            x, y, w, h = region
            img = img.crop((x, y, x + w, y + h))
        img.save(path)
        return True
    monkeypatch.setattr(capture, "_capture", cap)
    monkeypatch.setattr(capture, "_desktop_size", lambda: (400, 300))
    monkeypatch.setattr(capture, "_occlusion", lambda w: None)
    monkeypatch.setattr(capture, "_resolve_target",
                        lambda t: {"id": 7, "x": 100, "y": 50, "width": 200, "height": 150,
                                   "wm_class": "app"})
    monkeypatch.setattr(capture, "display_power", lambda: state["power"])
    monkeypatch.setattr(observations, "geometry", lambda: [(7, 100, 50, 200, 150)])
    monkeypatch.setattr(capture, "SHOT_DIR", tmp_path, raising=False)
    capture._LAST_CAPTURE.clear()
    return state


def test_no_stale_warning_while_displays_are_on(fake):
    out = capture.tool_screenshot({})
    assert "stale_warning" not in out


@pytest.mark.parametrize("mode", [1, 2, 3])
def test_stale_warning_while_displays_are_off(fake, mode):
    fake["power"] = mode
    out = capture.tool_screenshot({})
    assert "power-save" in out["stale_warning"]


def test_health_verdict_says_displays_are_off():
    verdict = server._health_verdict({"extension": "ACTIVE", "windows": 2, "atspi_apps": 3,
                                      "displays": "POWER-SAVE (off)",
                                      "desktop": "primary"})
    assert verdict.startswith("USABLE") and "powered down" in verdict
    assert "Screenshots" not in verdict.split(".")[-2]


def test_repeat_capture_is_noted_only_after_some_time(fake, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
    assert "repeat_capture" not in capture.tool_screenshot({})
    clock[0] += 2
    assert "repeat_capture" not in capture.tool_screenshot({})
    clock[0] += 60
    out = capture.tool_screenshot({})
    assert "byte-identical" in out["repeat_capture"]
    fake["screen"] = Image.new("RGB", (400, 300), (1, 2, 3))
    clock[0] += 60
    assert "repeat_capture" not in capture.tool_screenshot({})


def test_window_relative_region_for_screenshot(fake):
    out = capture.tool_screenshot({"window": "app", "region": [10, 20, 30, 40]})
    assert out["region"] == {"x": 110, "y": 70, "width": 30, "height": 40}


def test_zoom_enlarges_with_scale(fake):
    out = capture.tool_zoom({"region": [0, 0, 40, 30], "scale": 2})
    assert out["shown"]["dimensions"] == "80x60"


def test_zoom_from_observation_crops_the_same_instant(fake):
    shot = capture.tool_screenshot({})
    obs = shot["observation"]["id"]
    # the screen changes after the overview was taken
    fake["screen"] = Image.new("RGB", (400, 300), (255, 255, 255))
    out = capture.tool_zoom({"from_observation": obs, "region": [20, 0, 10, 10]})
    img = Image.open(io.BytesIO(base64.b64decode(out[capture._INLINE_KEY]["data"]))).convert("RGB")
    assert img.getpixel((2, 2)) != (255, 255, 255)
    assert out["from_observation"] == obs


def test_zoom_from_unknown_observation_is_stale(fake):
    with pytest.raises(ToolError) as e:
        capture.tool_zoom({"from_observation": "nope", "region": [0, 0, 5, 5]})
    assert e.value.code == "stale_observation"


def test_openai_token_estimate():
    assert capture._estimate_tokens_openai(1024, 1024) == 765   # OpenAI's own example
    assert capture._estimate_tokens_openai(2048, 4096) == 1105


def test_no_change_hints_suggest_hover_and_report_a_dead_process(monkeypatch):
    monkeypatch.setattr(capture, "process_state", lambda pid: {"pid": pid, "state": "gone"})
    hints = capture._no_change_hints({"x": 1, "y": 2}, {"pid": 99})
    assert "exited" in hints[0]
    assert any("hover_first" in h for h in hints)
    assert not any("hover_first" in h for h in
                   capture._no_change_hints({"x": 1, "y": 2, "hover_first": True}, None))


def test_process_state_reads_this_process():
    import os
    st = capture.process_state(os.getpid())
    assert st["pid"] == os.getpid() and "cpu_percent" in st
    assert capture.process_state(2 ** 22 + 12345)["state"] == "gone"
