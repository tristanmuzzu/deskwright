#!/usr/bin/env python3
"""wait_for: helpers do not satisfy window_exists; settled waits for stillness.

No desktop: the window list and the settle measurement are patched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import capture, shell


def test_a_one_pixel_helper_does_not_satisfy_window_exists(monkeypatch):
    monkeypatch.setattr(shell, "list_windows", lambda: [
        {"id": 1, "wm_class": "photoshop.exe", "title": "", "width": 1, "height": 1}])
    out = shell.tool_wait_for({"condition": "window_exists", "target": "photoshop",
                               "timeout": 0.3})
    assert out["met"] is False


def test_the_real_window_satisfies_it_and_reports_its_size(monkeypatch):
    monkeypatch.setattr(shell, "list_windows", lambda: [
        {"id": 1, "wm_class": "photoshop.exe", "title": "", "width": 1, "height": 1},
        {"id": 2, "wm_class": "photoshop.exe", "title": "Adobe Photoshop",
         "width": 1600, "height": 900}])
    out = shell.tool_wait_for({"condition": "window_exists", "target": "photoshop",
                               "timeout": 1})
    assert out["met"] and out["matched"][0]["id"] == 2
    assert out["matched"][0]["size"] == "1600x900"


def test_window_gone_still_counts_tiny_windows(monkeypatch):
    monkeypatch.setattr(shell, "list_windows", lambda: [
        {"id": 1, "wm_class": "x", "title": "", "width": 1, "height": 1}])
    assert shell.tool_wait_for({"condition": "window_gone", "target": "x",
                                "timeout": 0.3})["met"] is False


def test_popup_label_matches(monkeypatch):
    monkeypatch.setattr(shell, "list_windows", lambda: [
        {"id": 3, "wm_class": None, "title": "", "label": "gimp dialog 'Export Image'",
         "width": 500, "height": 400}])
    assert shell.tool_wait_for({"condition": "window_exists", "target": "Export Image",
                                "timeout": 0.5})["met"]


@pytest.mark.parametrize("settled", [True, False])
def test_settled_reports_the_measurement(monkeypatch, settled):
    monkeypatch.setattr(capture, "_settle", lambda path, region, t: {
        "settled": settled, "frames": 5, "fingerprint": b""})
    monkeypatch.setattr(capture, "display_power", lambda: 0)
    monkeypatch.setattr(shell, "_resolve_target",
                        lambda t: {"id": 1, "x": 0, "y": 0, "width": 10, "height": 10})
    out = shell.tool_wait_for({"condition": "settled", "target": 1, "timeout": 2})
    assert out["met"] is settled and out["frames"] == 5


def test_settled_is_never_met_while_the_displays_are_off(monkeypatch):
    monkeypatch.setattr(capture, "_settle", lambda path, region, t: {
        "settled": True, "frames": 5, "fingerprint": b""})
    monkeypatch.setattr(capture, "display_power", lambda: 3)
    out = shell.tool_wait_for({"condition": "settled", "timeout": 1})
    assert out["met"] is False and "power-save" in out["stale_warning"]
