#!/usr/bin/env python3
"""The action journal: evidence, not surveillance.

Pure in-process: the journal directory is pointed into a tmpdir via
XDG_STATE_HOME, nothing touches D-Bus, the compositor, or the screen.

Claims:
  1. record() appends one well-formed JSON line per call: ts with
     milliseconds, a stable session id, tool, redacted args, and an outcome
     summary carrying detail, the hit/miss verdict fields, and the shot's
     path + file sha256.
  2. Redaction: a text argument over 200 chars is truncated with a length
     note; inline image payloads (`__inline_image__`-like fields) are never
     stored, in args or outcome.
  3. A ToolError outcome is recorded with ok:false and its code.
  4. Rotation deletes journal files older than 14 days by their DATE-NAMED
     stem, and leaves everything else alone.
  5. record() is fire-and-forget: a failing os.open is swallowed.
  6. tool_journal tails in order -- oldest first, across day files, filtered
     to the current session unless session:"all".

    python3 -m pytest tests/test_journal.py    # or run it directly
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcu import journal
from wcu.errors import ToolError


@pytest.fixture
def jdir(tmp_path, monkeypatch) -> Path:
    """Point the journal into a tmpdir and return its directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "wayland-computer-use" / "journal"


def _read_lines(jdir: Path) -> list[dict]:
    entries = []
    for f in sorted(jdir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------- 1. shape
def test_append_shape_and_shot_hash(jdir, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"not-really-a-png")
    journal.record(
        "pointer_click",
        {"x": 100, "y": 200, "button": "left"},
        {"detail": "clicked something",
         "widget_focus": "focused",
         "look": {"changed": {"percent": 3.2},
                  "verdict": "the screen changed, so this landed on something",
                  "path": str(shot)}},
    )

    today = time.strftime("%Y-%m-%d")
    assert (jdir / f"{today}.jsonl").is_file()
    entries = _read_lines(jdir)
    assert len(entries) == 1
    e = entries[0]

    assert set(e) == {"ts", "session", "desktop", "tool", "args", "outcome"}
    # WHICH screen this happened on. Unpinned means the user's own; a pinned
    # server writes the headless session's name here instead.
    assert e["desktop"] == "primary"
    assert e["tool"] == "pointer_click"
    assert e["args"] == {"x": 100, "y": 200, "button": "left"}
    # iso timestamp with milliseconds
    assert "." in e["ts"] and len(e["ts"].split(".")[1].split("+")[0]) == 3
    # session id is pid-derived and stable across calls
    assert e["session"].startswith(f"{os.getpid()}-")
    assert e["session"] == journal._session()

    out = e["outcome"]
    assert out["ok"] is True
    assert out["detail"] == "clicked something"
    assert out["changed"] == {"percent": 3.2}          # lifted from look
    assert out["widget_focus"] == "focused"
    assert out["shot"]["path"] == str(shot)
    assert out["shot"]["sha256"] == hashlib.sha256(b"not-really-a-png").hexdigest()


def test_missing_shot_file_records_path_without_hash(jdir):
    journal.record("pointer_click", {"x": 1, "y": 1},
                   {"look": {"path": "/nonexistent/shot.png"}})
    out = _read_lines(jdir)[0]["outcome"]
    assert out["shot"] == {"path": "/nonexistent/shot.png"}


# ------------------------------------------------------------ 2. redaction
def test_long_text_argument_is_truncated_with_length_note(jdir):
    long = "x" * 500
    journal.record("type_text", {"text": long, "target": "editor"}, {"ok": 1})
    args = _read_lines(jdir)[0]["args"]
    assert len(args["text"]) < 300
    assert args["text"].startswith("x" * 200)
    assert "500 chars total" in args["text"]
    assert args["target"] == "editor"


def test_long_text_nested_in_steps_is_truncated(jdir):
    journal.record("do_steps",
                   {"steps": [{"do": "type", "text": "y" * 400}]}, {})
    step = _read_lines(jdir)[0]["args"]["steps"][0]
    assert "400 chars total" in step["text"]


def test_image_fields_never_stored(jdir):
    payload = {"data": "aaaa" * 50000, "media_type": "image/png"}
    journal.record(
        "pointer_click",
        {"x": 1, "y": 2, "__inline_image__": payload, "_image_data": "zzz"},
        {"detail": "d", "__inline_image__": payload, "__inline_image__s": [payload]},
    )
    raw = (jdir / (time.strftime("%Y-%m-%d") + ".jsonl")).read_text()
    assert "aaaa" not in raw
    assert "__inline_image__" not in raw
    assert "_image_data" not in raw
    e = _read_lines(jdir)[0]
    assert e["args"] == {"x": 1, "y": 2}


# ----------------------------------------------------------- 3. error path
def test_error_outcome_records_ok_false_and_code(jdir):
    journal.record("press_keys", {"combo": "ctrl+s", "target": "gedit"},
                   {"error": "the target window never reported focused",
                    "code": "focus_not_acquired"})
    out = _read_lines(jdir)[0]["outcome"]
    assert out["ok"] is False
    assert out["code"] == "focus_not_acquired"
    assert "never reported focused" in out["error"]


# ------------------------------------------------------------- 4. rotation
def test_rotation_deletes_old_dated_files_only(jdir):
    jdir.mkdir(parents=True)
    old = jdir / f"{journal._today() - timedelta(days=20):%Y-%m-%d}.jsonl"
    edge = jdir / f"{journal._today() - timedelta(days=14):%Y-%m-%d}.jsonl"
    fresh = jdir / f"{journal._today():%Y-%m-%d}.jsonl"
    stranger = jdir / "notes.jsonl"          # not date-named: not ours to delete
    for f in (old, edge, fresh, stranger):
        f.write_text("{}\n")

    journal._rotate()

    assert not old.exists()
    assert edge.exists()                      # exactly RETENTION_DAYS old: kept
    assert fresh.exists()
    assert stranger.exists()


def test_rotation_survives_missing_directory(jdir):
    assert not jdir.exists()
    journal._rotate()                         # must not raise


# ------------------------------------------------------- 5. fire-and-forget
def test_record_swallows_write_failure(jdir, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "open", boom)
    journal.record("pointer_click", {"x": 1, "y": 2}, {"detail": "d"})  # no raise
    monkeypatch.undo()
    assert not list(jdir.glob("*.jsonl"))     # nothing landed, nothing raised


def test_record_swallows_unserializable_and_bad_args(jdir):
    journal.record("t", {"x": object()}, {"detail": object()})   # default=str copes
    journal.record("t", None, None)                              # type: ignore[arg-type]
    # neither call may raise; whatever landed on disk must be valid JSON
    for e in _read_lines(jdir):
        assert e["tool"] == "t"


# ---------------------------------------------------------- 6. journal tool
def test_tool_journal_tail_and_order(jdir):
    for i in range(30):
        journal.record(f"tool_{i}", {"i": i}, {"detail": f"step {i}"})

    r = journal.tool_journal({"tail": 5})
    assert r["count"] == 5
    assert [e["tool"] for e in r["entries"]] == [f"tool_{i}" for i in range(25, 30)]

    r = journal.tool_journal({})              # default tail is 20
    assert r["count"] == 20
    assert r["entries"][0]["tool"] == "tool_10"
    assert r["entries"][-1]["tool"] == "tool_29"
    assert r["session_id"] == journal._session()


def test_tool_journal_spans_day_files_in_order(jdir):
    jdir.mkdir(parents=True)
    yesterday = jdir / f"{journal._today() - timedelta(days=1):%Y-%m-%d}.jsonl"
    lines = [json.dumps({"ts": "2026-08-22T10:00:00.000+00:00",
                         "session": journal._session(),
                         "tool": f"old_{i}", "args": {}, "outcome": {"ok": True}})
             for i in range(3)]
    yesterday.write_text("\n".join(lines) + "\nnot json at all\n")

    journal.record("new_0", {}, {})
    r = journal.tool_journal({"tail": 4})
    # oldest first, yesterday's before today's, corrupt line skipped not fatal
    tools = [e["tool"] for e in r["entries"]]
    assert tools == ["old_0", "old_1", "old_2", "new_0"]
    assert r["skipped_corrupt_lines"] == 1


def test_tool_journal_session_filter(jdir):
    journal.record("mine", {}, {})
    today_file = jdir / (time.strftime("%Y-%m-%d") + ".jsonl")
    foreign = {"ts": "2026-08-23T00:00:00.000+00:00", "session": "999-0",
               "tool": "theirs", "args": {}, "outcome": {"ok": True}}
    with open(today_file, "a") as f:
        f.write(json.dumps(foreign) + "\n")

    current = journal.tool_journal({"session": "current"})
    assert [e["tool"] for e in current["entries"]] == ["mine"]

    everything = journal.tool_journal({"session": "all"})
    assert [e["tool"] for e in everything["entries"]] == ["mine", "theirs"]


def test_tool_journal_empty_and_bad_args(jdir):
    r = journal.tool_journal({})
    assert r["count"] == 0 and r["entries"] == []

    with pytest.raises(ToolError) as ei:
        journal.tool_journal({"session": "yesterday"})
    assert ei.value.code == "bad_args"

    with pytest.raises(ToolError) as ei:
        journal.tool_journal({"tail": "many"})
    assert ei.value.code == "bad_args"


def test_should_journal_seam():
    assert journal.SHOULD_JOURNAL("pointer_click", False) is True
    assert journal.SHOULD_JOURNAL("screenshot", True) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_desktop_field_names_the_headless_session(jdir, monkeypatch):
    """With several virtual desktops live, 'a click happened' is only a
    reviewable record if it says where (2026-08-27)."""
    monkeypatch.setenv("WCU_HEADLESS", "1")
    monkeypatch.setenv("WCU_HEADLESS_NAME", "work")
    journal.record("pointer_click", {"x": 1, "y": 2}, {"detail": "ok"})
    assert _read_lines(jdir)[-1]["desktop"] == "work"


def test_desktop_field_falls_back_when_only_the_flag_is_set(jdir, monkeypatch):
    """A server pinned by an older wcu-headless env has WCU_HEADLESS but no
    name; it must still not be recorded as the user's screen."""
    monkeypatch.setenv("WCU_HEADLESS", "1")
    monkeypatch.delenv("WCU_HEADLESS_NAME", raising=False)
    journal.record("pointer_click", {"x": 1, "y": 2}, {"detail": "ok"})
    assert _read_lines(jdir)[-1]["desktop"] == "headless"
