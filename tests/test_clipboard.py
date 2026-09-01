#!/usr/bin/env python3
"""Proof that the clipboard tools write what they say and read what is there,
plus the validation paths of hold_key and pointer_drag.

In-process against deskwright.input; the clipboard is exercised through real
wl-copy/wl-paste subprocesses and the real extension. Everything written here
is tagged 'wcu-test-...', the prior clipboard TEXT is saved first and restored
at the end (a non-text offer cannot be saved from here -- the first write
destroys it either way, which is why only text is promised back).

hold_key and pointer_drag get validation-path checks only: every case below
must fail BEFORE any key or button is injected, so this file never types,
clicks or holds anything.

    ./tests/test_clipboard.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright import input as inp
from deskwright.errors import ToolError

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        results.append((PASS, name, detail or ""))
    except AssertionError as exc:
        results.append((FAIL, name, str(exc)))
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))


def expect_tool_error(fn, args: dict, code: str, needle: str = "") -> str:
    try:
        fn(args)
    except ToolError as e:
        assert e.code == code, f"refused with code {e.code!r}, wanted {code!r}: {e}"
        assert needle in str(e), f"refused for the wrong reason: {e}"
        return f"[{e.code}] {str(e)[:60]}"
    raise AssertionError(f"{args} was accepted instead of refused")


def save_prior() -> str | None:
    proc = subprocess.run(["wl-paste", "--no-newline"], capture_output=True,
                          check=False, timeout=3)
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode()
    except UnicodeDecodeError:
        return None


def restore_prior(prior: str | None) -> None:
    if prior is not None:
        subprocess.run(["wl-copy"], input=prior.encode(), check=False, timeout=3)
    else:
        subprocess.run(["wl-copy", "--clear"], check=False, timeout=3)


# --------------------------------------------------------------- clipboard
def test_write_text_verified(tag: str) -> str:
    r = inp.tool_clipboard_write({"text": tag})
    assert r["verified"] is True, f"write not verified: {r}"
    return f'via {r["via"]}'


def test_read_back(tag: str) -> str:
    r = inp.tool_clipboard_read({})
    assert r["text"] == tag, f"read {r.get('text')!r}, wanted {tag!r}"
    return f'{r["characters"]} characters'


def test_leading_dash() -> str:
    """Text starting with '-' must not be eaten as a gdbus option."""
    text = "--wcu-test-leading-dash"
    r = inp.tool_clipboard_write({"text": text})
    assert r["verified"] is True, f"write not verified: {r}"
    got = inp.tool_clipboard_read({})["text"]
    assert got == text, f"read {got!r}, wanted {text!r}"
    return f'{text!r} survived, via {r["via"]}'


def test_awkward_text() -> str:
    """Quotes, newlines and backslashes must survive the argv trip.

    gdbus re-quotes unparseable string args without escaping backslashes, so
    '\\b' used to arrive as byte 0x08 -- this is the regression test for the
    GVariant literal encoding that fixed it.
    """
    text = "wcu-test 'single'\nline2 \"double\" \\backslash \\u0041"
    r = inp.tool_clipboard_write({"text": text})
    assert r["verified"] is True, f"write not verified: {r}"
    got = inp.tool_clipboard_read({})["text"]
    assert got == text, f"read {got!r}, wanted {text!r}"
    return "quotes, a newline and backslash escapes all round-tripped"


def test_fully_quoted_text() -> str:
    """Text that IS a quoted string must keep its quotes.

    Without self-serialising, g_variant_parse succeeds on \"'hello'\" directly
    and the clipboard silently receives hello without the quotes.
    """
    text = "'wcu-test-fully-quoted'"
    r = inp.tool_clipboard_write({"text": text})
    assert r["verified"] is True, f"write not verified: {r}"
    got = inp.tool_clipboard_read({})["text"]
    assert got == text, f"read {got!r}, wanted {text!r}"
    return f"{text} kept its quotes"


def test_list_types() -> str:
    r = inp.tool_clipboard_read({"types": True})
    assert isinstance(r.get("types"), list) and r["types"], f"no types: {r}"
    assert any("text" in t for t in r["types"]), f"no text type in {r['types']}"
    return f'{r["count"]} types, e.g. {r["types"][0]}'


def test_write_file(tmp: Path) -> str:
    payload = b"wcu-test-file-payload \x00binary\xff"
    file = tmp / "wcu-test-clip.bin"
    file.write_bytes(payload)
    r = inp.tool_clipboard_write({"path": str(file),
                                  "mimetype": "application/octet-stream"})
    assert r["verified"] is True, f"file write not verified: {r}"
    assert r["bytes"] == len(payload)
    return f'{r["bytes"]} bytes ({payload.count(0)} NUL) via {r["via"]}'


def test_write_bad_args() -> str:
    seen = [
        expect_tool_error(inp.tool_clipboard_write, {}, "bad_args",
                          "exactly one"),
        expect_tool_error(inp.tool_clipboard_write,
                          {"text": "x", "path": "/tmp/x"}, "bad_args",
                          "exactly one"),
        expect_tool_error(inp.tool_clipboard_write, {"text": ""}, "bad_args",
                          "non-empty"),
        expect_tool_error(inp.tool_clipboard_write,
                          {"path": "/tmp/wcu-no-such"}, "bad_args", "mimetype"),
        expect_tool_error(inp.tool_clipboard_write,
                          {"path": "/tmp/wcu-no-such-file-anywhere.png",
                           "mimetype": "image/png"}, "bad_args",
                          "does not exist"),
    ]
    return f"{len(seen)} malformed writes refused"


def test_empty_clipboard_is_clean() -> str:
    subprocess.run(["wl-copy", "--clear"], check=False, timeout=3)
    time.sleep(0.2)
    r = inp.tool_clipboard_read({})
    if r.get("text") is not None:
        # A clipboard manager re-offered content the moment it was cleared.
        # That is the environment interfering, not the tool failing.
        return f"skipped: a clipboard manager restored {r['text'][:30]!r}"
    assert r.get("empty") is True, f"empty clipboard was not clean: {r}"
    types = inp.tool_clipboard_read({"types": True})
    assert types.get("empty") is True or types.get("types") == [], (
        f"empty types listing was not clean: {types}")
    return "read and types both report empty as a state, not an error"


# --------------------------------------------- hold_key / drag validation
def test_hold_key_bad_args() -> str:
    seen = [
        expect_tool_error(inp.tool_hold_key, {}, "bad_args", "key is required"),
        expect_tool_error(inp.tool_hold_key, {"key": "shift"}, "bad_args",
                          "seconds is required"),
        expect_tool_error(inp.tool_hold_key,
                          {"key": "shift", "seconds": "long"}, "bad_args",
                          "must be a number"),
        expect_tool_error(inp.tool_hold_key,
                          {"key": "shift", "seconds": True}, "bad_args",
                          "must be a number"),
        expect_tool_error(inp.tool_hold_key, {"key": "shift", "seconds": 0},
                          "bad_args", "between"),
        expect_tool_error(inp.tool_hold_key, {"key": "shift", "seconds": 301},
                          "bad_args", "between"),
        expect_tool_error(inp.tool_hold_key,
                          {"key": "ctrl+c", "seconds": 1}, "bad_args",
                          "press_keys"),
        expect_tool_error(inp.tool_hold_key,
                          {"key": "qqqq", "seconds": 1}, "bad_args",
                          "unknown key"),
    ]
    return f"{len(seen)} malformed holds refused before any injection"


def test_hold_key_missing_target() -> str:
    return expect_tool_error(
        inp.tool_hold_key,
        {"key": "shift", "seconds": 0.1, "target": "wcu-no-such-window-xyz"},
        "window_not_found")


def test_drag_bad_args() -> str:
    seen = [
        expect_tool_error(inp.tool_pointer_drag, {}, "bad_args", "required"),
        expect_tool_error(inp.tool_pointer_drag,
                          {"from_x": 10, "from_y": 10, "to_x": 50},
                          "bad_args", "required"),
        expect_tool_error(inp.tool_pointer_drag,
                          {"from_x": "left", "from_y": 10,
                           "to_x": 50, "to_y": 50},
                          "bad_args", "must be a number"),
    ]
    return f"{len(seen)} malformed drags refused"


def test_drag_missing_expect_window() -> str:
    """The guard runs before the pointer moves, so nothing is dragged."""
    return expect_tool_error(
        inp.tool_pointer_drag,
        {"from_x": 10, "from_y": 10, "to_x": 50, "to_y": 50,
         "expect_window": "wcu-no-such-window-xyz"},
        "window_not_found")


def main() -> int:
    if not os.environ.get("WAYLAND_DISPLAY") or \
            not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        print("SKIP  no Wayland session bus; these tests need the graphical "
              "session")
        return 0

    prior = save_prior()
    tag = f"wcu-test-{time.time():.0f}-roundtrip"
    try:
        with tempfile.TemporaryDirectory(prefix="wcu-clip-") as tmpdir:
            tmp = Path(tmpdir)
            check("write text is verified", lambda: test_write_text_verified(tag))
            check("clipboard_read returns it", lambda: test_read_back(tag))
            check("leading-dash text survives gdbus", test_leading_dash)
            check("quotes and newlines round-trip", test_awkward_text)
            check("fully-quoted text keeps its quotes", test_fully_quoted_text)
            check("types are listed", test_list_types)
            check("file write round-trips bytes", lambda: test_write_file(tmp))
            check("malformed writes are refused", test_write_bad_args)
            check("an empty clipboard is a clean result",
                  test_empty_clipboard_is_clean)
            check("hold_key refuses bad args", test_hold_key_bad_args)
            check("hold_key refuses a missing target",
                  test_hold_key_missing_target)
            check("drag refuses bad args", test_drag_bad_args)
            check("drag refuses a missing expect_window",
                  test_drag_missing_expect_window)
    finally:
        restore_prior(prior)

    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failed}/{len(results)} passed"
          + ("" if prior is None else "; prior clipboard text restored"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
