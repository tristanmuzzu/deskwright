#!/usr/bin/env python3
"""End-to-end proof that the MCP server does real work on this desktop.

Not a demo. A demo shows a call returning success. Every assertion here checks the
DESKTOP afterwards, because on this machine the two come apart constantly:
`ydotool` reports success whether or not the characters landed anywhere, and
AT-SPI `do_action` returned true on a button whose dialog never appeared (it was a
portal in another process).

Two tiers, because half the stack survives a locked screen and half does not.

  TIER 1 -- AT-SPI only. Works even while the screen is locked, which is the
  whole reason to prefer it. Launches gnome-text-editor, writes text into it with
  no focus and no keyboard, reads the widget back to prove the text is there,
  finds a deeply-nested button, presses it, and proves the popover opened by
  watching the accessibility tree grow and shrink again.

  TIER 2 -- needs the migration-helpers extension, which gnome-shell unloads
  while the screen is locked. Window enumeration, screenshots, and focus-verified
  typing. Skipped with a precise reason rather than failing mysteriously.

Guards are asserted in both tiers: typing with no target, pressing a widget
without stating what you expect, a widget whose identity moved, and Ctrl+Alt+F2.

KNOWN FRAGILITY, MEASURED 2026-08-17

"injected keystrokes actually landed" fails whenever gnome-text-editor already
has other tabs open, which on a real desktop it usually does. TIER 1 writes
through AT-SPI, which addresses a document directly; typed keys go to whatever
tab is VISIBLE; and `ui_read_text` then reads the first text widget in the
tree, which need be neither. The failure message is "the widget gained ''".

It is not the injection. With a single-tab editor the same sequence
(ui_set_text, then type_text) verifies itself, and the characters are there in
a screenshot. It also fails identically with ydotool and with compositor
keysyms, which is the tell: two unrelated injection paths cannot be broken in
the same way at the same moment. Fixing it means addressing ONE document
rather than "the app's first text widget".

Half of that landed on 2026-08-22: `_find_text_widget` now prefers the widget
AT-SPI reports as FOCUSED, which is the one keystrokes go to. That fixes the
readback for an editor with several tabs. It does not fix this test, because
TIER 1 here writes through `ui_set_text` to a document that is not the focused
one, so the two halves still address different tabs. The remaining half is
`ui_set_text` and `type_text` taking the same document identity.

    ./tests/test_e2e_real_task.py
    ./tests/test_e2e_real_task.py --keep     # leave the editor open to look at
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = REPO / "mcp_server.py"
EDITOR_APP = "gnome-text-editor"
EDITOR_SETTLE_S = 9.0
# Its wm_class is 'org.gnome.TextEditor' while its AT-SPI app name is
# 'gnome-text-editor'. Matching on the literal 'text-editor' missed the window
# entirely, so compare with separators stripped.
EDITOR_WM_CLASS_HINT = "texteditor"


def normalise(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())

PASSES: list[str] = []
FAILURES: list[str] = []


class Client:
    """Minimal MCP stdio client -- talks to the server the way Claude Code does."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=dict(os.environ),
        )
        self._id = 0
        init = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1"},
        })
        assert "result" in init, f"initialize failed: {init}"
        self.notify("notifications/initialized")

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        want = self._id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                err = (self.proc.stderr.read() or "").strip()
                raise RuntimeError(f"server died (rc={self.proc.poll()}): {err[:400]}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want:
                return msg

    def call(self, tool: str, args: dict | None = None):
        """Return (ok, payload); payload is parsed JSON when the tool returned JSON."""
        msg = self.request("tools/call", {"name": tool, "arguments": args or {}})
        result = msg.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content") or [])
        if result.get("isError"):
            return False, text
        try:
            return True, json.loads(text)
        except json.JSONDecodeError:
            return True, text

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait(timeout=5)


def check(label: str, condition: bool, detail: str = "") -> bool:
    (PASSES if condition else FAILURES).append(label)
    print(f"{'PASS' if condition else 'FAIL'}  {label:<40} {str(detail)[:110]}")
    return bool(condition)


def app_node_count(client: Client) -> int:
    """Total nodes in the editor's accessibility tree, for before/after diffs."""
    ok, payload = client.call("ui_tree", {"app": EDITOR_APP, "depth": 30})
    return payload.get("nodes", -1) if ok and isinstance(payload, dict) else -1


# =========================================================================
# TIER 1 -- AT-SPI only, survives a locked screen
# =========================================================================
def tier1_atspi(client: Client) -> None:
    print("\nTIER 1 -- AT-SPI (works while the screen is locked)")

    ok, apps = client.call("ui_apps")
    check("ui_apps enumerates the bus", ok and apps.get("count", 0) >= 3,
          f'{apps.get("count") if ok else apps} apps')

    marker = f"wcu-e2e-{uuid.uuid4().hex[:8]}"

    # Write with no focus and no keyboard at all.
    ok, res = client.call("ui_set_text", {"app": EDITOR_APP, "text": marker,
                                          "replace": True})
    wrote = check("ui_set_text writes without focus", ok and res.get("verified"),
                  res.get("detail") if ok else res)

    # THE proof: read the widget back out of the tree.
    ok, res = client.call("ui_read_text", {"app": EDITOR_APP})
    content = res.get("text", "") if ok else ""
    check("the text is really IN the widget", ok and marker in content,
          f"{len(content)} chars, marker present: {marker in content}")

    # replace:true must have cleared what was there, not appended.
    check("replace cleared prior content", wrote and content.strip() == marker,
          f"widget holds {content[:40]!r}")

    # A deeply nested widget: GTK4 puts these past depth 15, where a depth-8
    # search finds literally nothing.
    ok, found = client.call("ui_find", {"text": "Main Menu", "app": EDITOR_APP,
                                        "actionable_only": True})
    button = None
    if ok and isinstance(found, dict) and found.get("results"):
        button = next((r for r in found["results"] if r["name"] == "Main Menu"),
                      found["results"][0])
    if not check("ui_find reaches a deeply nested widget", button is not None,
                 f'{button["path"]} (depth {button["path"].count("/")})' if button else "not found"):
        return

    # Identity guard: refuse to act when the path no longer means what you think.
    ok, msg = client.call("ui_press", {"path": button["path"],
                                       "expect_name": "Not This Widget At All"})
    check("refuses a widget whose identity moved",
          not ok and "refusing to act" in str(msg), str(msg)[:80])

    # Press for real, and prove the UI changed rather than trusting do_action.
    before = app_node_count(client)
    ok, res = client.call("ui_press", {"path": button["path"],
                                       "expect_name": "Main Menu",
                                       "expect_role": button["role"]})
    check("ui_press invokes the real action", ok,
          res.get("detail") if ok else res)
    time.sleep(1.5)
    after = app_node_count(client)
    check("the popover actually opened", after > before,
          f"tree grew {before} -> {after} nodes")

    # And close it again, so the test leaves no menu hanging open.
    client.call("ui_press", {"path": button["path"], "expect_name": "Main Menu"})
    time.sleep(1.2)
    closed = app_node_count(client)
    check("the popover closed again", closed < after, f"tree back to {closed} nodes")


# =========================================================================
# TIER 2 -- needs the gnome-shell extension
# =========================================================================
def tier2_extension(client: Client, health: dict) -> None:
    print("\nTIER 2 -- gnome-shell extension (windows, screenshots, injection)")
    if health.get("extension") != "ACTIVE":
        print("SKIPPED. Server diagnosis:")
        print(f"  {health.get('extension_diagnosis', 'unknown')}")
        return

    ok, payload = client.call("list_windows")
    windows = payload.get("windows", []) if ok else []
    check("list_windows enumerates windows", ok and len(windows) >= 1,
          f"{len(windows)} windows")

    window = next((w for w in windows
                   if EDITOR_WM_CLASS_HINT in normalise(w.get("wm_class"))), None)
    if not check("the editor window is visible to the shell", window is not None,
                 f'id={window["id"]}' if window else "not found"):
        return

    ok, res = client.call("activate_window", {"target": window["id"]})
    check("focus is confirmed, not assumed", ok and "focus" in json.dumps(res),
          res.get("detail") if ok else res)

    # Focus-verified injection, then verified by reading the widget back.
    typed = f" typed-{uuid.uuid4().hex[:6]}"
    ok, res = client.call("type_text", {"text": typed, "target": window["id"]})
    check("type_text reports proven focus", ok and isinstance(res, dict),
          res.get("focus") if ok else res)
    time.sleep(1.2)
    ok, res = client.call("ui_read_text", {"app": EDITOR_APP})
    check("injected keystrokes actually landed",
          ok and typed.strip() in res.get("text", ""),
          f'widget holds {res.get("text", "")[:40]!r}' if ok else res)

    shot = Path("/tmp") / f"wcu-e2e-{uuid.uuid4().hex[:6]}.png"
    ok, res = client.call("screenshot", {"path": str(shot)})
    check("screenshot is a real PNG with real dimensions",
          ok and res.get("bytes", 0) > 10000 and "x" in (res.get("dimensions") or ""),
          json.dumps(res) if ok else res)
    shot.unlink(missing_ok=True)

    ok, msg = client.call("press_keys", {"combo": "ctrl+alt+f2",
                                         "target": window["id"]})
    check("refuses ctrl+alt+f2 (VT switch)",
          not ok and "switch-to-session" in str(msg), str(msg)[:80])


def guards_always(client: Client) -> None:
    print("\nGUARDS")
    ok, msg = client.call("type_text", {"text": "must not be typed"})
    check("refuses typing with no target",
          not ok and "focus-blind" in str(msg), str(msg)[:80])

    ok, msg = client.call("ui_press", {"path": f"{EDITOR_APP}/0"})
    check("refuses ui_press with no expectation",
          not ok and "expect_name or expect_role" in str(msg), str(msg)[:80])

    ok, msg = client.call("press_keys", {"combo": "ctrl+alt+f5", "target": 1})
    check("refuses the VT combo before resolving a target",
          not ok and "switch-to-session" in str(msg), str(msg)[:80])


def main() -> int:
    keep = "--keep" in sys.argv
    client = Client()

    # Open a DEDICATED file rather than a blank window. gnome-text-editor is
    # DBusActivatable, so --new-window forwards to the running instance and one
    # AT-SPI app node covers all its windows -- meaning ui_set_text{replace:true}
    # could land on, and wipe, an unsaved draft Tristan cared about. A scratch file
    # makes the target unambiguous and the test harmless.
    scratch = Path(tempfile.gettempdir()) / f"wcu-e2e-{uuid.uuid4().hex[:8]}.txt"
    scratch.write_text("scratch file for the wayland-computer-use e2e test\n")
    editor = subprocess.Popen(["gnome-text-editor", str(scratch)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(EDITOR_SETTLE_S)

        ok, health = client.call("desktop_health")
        check("desktop_health answers", ok and isinstance(health, dict),
              f'extension={health.get("extension")} session={health.get("session_type")}'
              if ok else health)

        tier1_atspi(client)
        tier2_extension(client, health if isinstance(health, dict) else {})
        guards_always(client)

        total = len(PASSES) + len(FAILURES)
        print(f"\n{len(PASSES)}/{total} passed")
        if FAILURES:
            print("failed: " + ", ".join(FAILURES))
        return 1 if FAILURES else 0
    finally:
        if keep:
            print(f"--keep: editor left open on {scratch}")
        else:
            editor.terminate()
            try:
                editor.wait(timeout=8)
            except subprocess.TimeoutExpired:
                editor.kill()
            # Only the scratch file is removed. Nothing else the editor holds is
            # touched, because the app instance may be shared with real windows.
            scratch.unlink(missing_ok=True)
        client.close()


if __name__ == "__main__":
    sys.exit(main())
