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

DOCUMENT IDENTITY -- fragility measured 2026-08-17, fixed 2026-08-23

This test used to fail whenever gnome-text-editor already had other tabs open,
which on a real desktop it usually does. Three independent widget picks were in
play: TIER 1 wrote through AT-SPI to "the app's first text widget", typed keys
went to whatever tab was VISIBLE, and `ui_read_text` re-picked its own widget.
No shared document, and the failure message -- "the widget gained ''" --
blamed the injection, which was innocent (it failed identically through
ydotool and compositor keysyms, which two unrelated paths cannot do at once).

The fix is one explicit identity. Half landed 2026-08-22: `_find_text_widget`
prefers the widget AT-SPI reports as FOCUSED, on the read side. The other half
landed 2026-08-23: `ui_set_text` shares that picker, and BOTH tools now return
the resolved `path` of the widget they touched. This test uses it: TIER 1
passes the path returned by `ui_set_text` into its readback, and TIER 2 pins
the focused document's path before typing and reads back through it.

Remaining honest caveat: a pinned path is an index path, valid only while the
tree is unchanged. A tab opened or closed between write and readback
invalidates the pin -- deliberately: `_resolve_path` raises `widget_missing`
rather than silently reading whichever document now sits at those indices, so
the failure names itself instead of masquerading as lost keystrokes. And
focused-first still needs something to BE focused: if the editor never held
focus, the picker falls back to the first editable widget -- the returned path
makes that pick visible, it does not make it the visible tab.

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

# Pin THIS process to the session under test, not just the server subprocess.
# The server pins itself from WCU_SESSION, but the editor below is spawned by
# this test's Popen with this test's environment -- unpinned, it opens on the
# USER'S desktop while the server looks for it on the headless one, which is
# exactly what happened on the first headless run (editor flashing on the real
# screen, every tier failing app_not_on_bus). Importing mcp_server runs the
# same _resolve_session() the server runs; with WCU_SESSION unset it is a
# no-op and the test behaves exactly as before.
sys.path.insert(0, str(REPO))
import mcp_server  # noqa: E402,F401  (imported for its session pinning)

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

    # The write names the document it landed in. That path is the whole
    # document-identity fix: the readback below addresses THIS document, not
    # whichever text widget a fresh pick would fancy.
    doc_path = res.get("path") if ok and isinstance(res, dict) else None
    check("ui_set_text names the document it wrote", bool(doc_path),
          f"path={doc_path}")

    # THE proof: read the SAME widget back out of the tree, pinned by path.
    ok, res = client.call("ui_read_text",
                          {"path": doc_path} if doc_path
                          else {"app": EDITOR_APP})
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

    # Pin the FOCUSED document before typing. Keystrokes go to the keyboard
    # focus, so the readback must address that exact document -- re-picking
    # after the fact is how this test used to blame the innocent injection.
    ok, res = client.call("ui_read_text", {"app": EDITOR_APP})
    focused = res if ok and isinstance(res, dict) else {}
    focused_path = focused.get("path")
    # The ADDRESS is the contract; the focused flag is honest state, not a
    # requirement. Measured 2026-08-23: after Tier 1's popover dance NO
    # widget holds the keyboard even though the window is focused -- that
    # void is real, and type_text below recovers from it (grab, then
    # Tab-cycling) rather than this test pretending it cannot happen.
    check("the document has an address",
          bool(focused_path),
          f'path={focused_path} focused={focused.get("focused", "?")}')

    # Focus-verified injection, then verified by reading the widget back.
    typed = f" typed-{uuid.uuid4().hex[:6]}"
    ok, res = client.call("type_text", {"text": typed, "target": window["id"]})
    check("type_text reports proven focus", ok and isinstance(res, dict),
          res.get("focus") if ok else res)
    time.sleep(1.2)
    ok, res = client.call("ui_read_text",
                          {"path": focused_path} if focused_path
                          else {"app": EDITOR_APP})
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

    # Open a DEDICATED file in a STANDALONE instance. Without --standalone,
    # gnome-text-editor is DBusActivatable: the Popen is only a messenger to
    # the running instance, terminate() kills the messenger and not the
    # editor, and every run's tabs accumulate in an instance that also
    # restores its previous session -- which is how this test spent a day
    # failing against ghost tabs holding earlier runs' filenames (measured
    # 2026-08-23: readback found 'wcu-e2e-<old hex>' from an instance three
    # runs old). --standalone implies --ignore-session: a fresh process, a
    # tree with exactly one document, and a terminate() that actually ends
    # it. A scratch file keeps the target unambiguous and the test harmless
    # either way.
    scratch = Path(tempfile.gettempdir()) / f"wcu-e2e-{uuid.uuid4().hex[:8]}.txt"
    scratch.write_text("scratch file for the wayland-computer-use e2e test\n")
    # A throwaway XDG_DATA_HOME, because --standalone is NOT stateless: the
    # editor still restores every unsaved draft from
    # ~/.local/share/org.gnome.TextEditor/, and this test's own terminated
    # runs are exactly what leaves such drafts. Measured 2026-08-25: thirty
    # accumulated wcu-e2e-* drafts meant every launch opened extra windows,
    # the tree pegged at the 400-node cap (popover growth invisible), and
    # typed keystrokes landed in the focused window while the readback pin
    # resolved into a restored one. Isolated state: one window, every run.
    data_home = Path(tempfile.mkdtemp(prefix="wcu-e2e-xdg-"))
    editor = subprocess.Popen(["gnome-text-editor", "--standalone", str(scratch)],
                              env={**os.environ, "XDG_DATA_HOME": str(data_home)},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait for the editor to BE on the bus, not for a guessed duration.
        # The fixed 9 s sleep this replaces was tuned on a warm desktop; on a
        # cold headless session (services still dbus-activating) first launch
        # measured 15-20 s, and every tier-1 check then failed app_not_on_bus.
        # The cap is generous because a hit exits the loop the moment it lands.
        deadline = time.monotonic() + EDITOR_SETTLE_S * 5
        while time.monotonic() < deadline:
            ok, apps = client.call("ui_apps")
            names = [a.get("name", "") for a in apps.get("apps", [])] if ok else []
            if any(EDITOR_APP in n for n in names):
                break
            time.sleep(1.0)
        time.sleep(2.0)                       # let the window map and settle

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
            # Only the scratch file and the throwaway state dir are removed.
            # Nothing else the editor holds is touched, because the app
            # instance may be shared with real windows.
            scratch.unlink(missing_ok=True)
            import shutil
            shutil.rmtree(data_home, ignore_errors=True)
        client.close()


if __name__ == "__main__":
    sys.exit(main())
