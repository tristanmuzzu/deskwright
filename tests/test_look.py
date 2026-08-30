#!/usr/bin/env python3
"""Proof that the tools SHOW you things, and that they can tell a hit from a miss.

Every claim here was a measurement before it was a test. The numbers in the
comments are what this machine actually did on 2026-08-22, and they are the
reason the thresholds are what they are -- the first threshold tried was five
times too high and reported every real button press as "nothing happened".

Uses gnome-calculator as the target: it is safe to click at random, its display
changes visibly when it does, and nothing in it belongs to anybody. It is
launched here and closed at the end.

    ./tests/test_look.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mcpdrv import Server

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str) -> None:
    CHECKS.append((label, ok, detail))


def calculator(server: Server) -> dict:
    """The TOPMOST calculator window.

    list_windows is bottom of the stack first, so taking the first match picks
    the oldest instance. With a stray calculator left over from an earlier run
    that is the one underneath, every click then gets refused by expect_window
    for landing on the other one -- which is correct behaviour and a broken
    test.
    """
    for _ in range(20):
        found = [w for w in server.call("list_windows", {})["windows"]
                 if "Calculator" in (w["wm_class"] or "")]
        if len(found) > 1:
            # Two calculator windows are cascaded 50px apart and overlap. Every
            # click then gets refused by expect_window for landing on the other
            # one, which is the guard working exactly as intended and a test
            # that cannot say so. Skip with the reason rather than fail with
            # four mysteries.
            raise SystemExit(
                f"SKIP: {len(found)} calculator windows are open "
                f"({', '.join(str(w['id']) for w in found)}). Close the extra one; "
                "this test needs an unambiguous target."
            )
        if found:
            return found[0]
        time.sleep(0.5)
    raise SystemExit("SKIP: gnome-calculator never appeared")


def focused_calculator(server: Server) -> dict | None:
    for _ in range(10):
        for w in server.call("list_windows", {})["windows"]:
            if "Calculator" in (w["wm_class"] or "") and w.get("focused"):
                return w
        time.sleep(0.3)
    return None


def main() -> int:
    # Check BEFORE launching, not after. Checking afterwards is a race: the
    # stray window is already there and gets returned on the first look, while
    # this test's own window appears a second later, and then every click is
    # refused for landing on the wrong one.
    with Server() as probe:
        existing = [w for w in probe.call("list_windows", {})["windows"]
                    if "Calculator" in (w["wm_class"] or "")]
    if existing:
        raise SystemExit(
            f"SKIP: {len(existing)} calculator window(s) already open "
            f"({', '.join(str(w['id']) for w in existing)}). This test needs an "
            "unambiguous target -- close them and run it again."
        )

    launched = subprocess.Popen(["gnome-calculator"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with Server() as s:
            win = calculator(s)
            s.call("activate_window", {"target": win["id"], "look": False})
            # Re-resolve AFTER activating, and take the one that actually has
            # focus. gnome-calculator is single-instance: launching it again
            # raises whichever window already existed rather than making a new
            # one, so the window this test then drives need not be the window it
            # thought it launched. Every click was correctly refused by
            # expect_window for landing on the other instance.
            win = focused_calculator(s) or win
            wid, wx, wy = win["id"], win["x"], win["y"]
            # A window that has just been mapped is still animating in, and one
            # run in six saw region_changed fire on the tail of that. Waiting is
            # the honest fix here: the thing being tested is whether a QUIET
            # screen reads as quiet, so the screen has to be quiet first.
            time.sleep(1.2)

            # -- an image comes back in the reply, not a path to one -----------
            shot = s.call("screenshot", {"window": wid})
            check("screenshot returns the image inline",
                  "image" in shot["_blocks"] and shot["_image_bytes"] > 2000,
                  f'blocks={shot["_blocks"]} bytes={shot["_image_bytes"]}')

            # The exact wire shape, asserted against the raw protocol reply.
            # MCP ImageContent is FLAT -- {type, data, mimeType}. The first
            # version of this server sent the Anthropic Messages API shape
            # instead ({type, source:{type, media_type, data}}), copied from a
            # transcript, which shows what the host produces AFTER converting.
            # Every test passed, because the driver read it back the same wrong
            # way. Only the real host rejected it, and only once it was live.
            raw = s._rpc("tools/call", {"name": "screenshot",
                                        "arguments": {"window": wid}})
            blocks = (raw.get("result") or {}).get("content") or []
            images = [b for b in blocks if b.get("type") == "image"]
            check("the image block is MCP ImageContent, not the API's shape",
                  bool(images) and set(images[0]) == {"type", "data", "mimeType"},
                  f"keys={sorted(images[0]) if images else None}")

            # A window costs a fraction of the whole screen. Measured: 295
            # tokens for this window against 1843 for the desktop.
            full = s.call("screenshot", {})
            win_tokens = (shot.get("shown") or {}).get("tokens_estimate", 0)
            full_tokens = (full.get("shown") or {}).get("tokens_estimate", 0)
            check("a window costs far less than the screen",
                  0 < win_tokens < full_tokens / 2,
                  f"window {win_tokens} tokens, screen {full_tokens}")

            # -- window + region is a window-relative rectangle ---------------
            strip = s.call("screenshot", {"window": wid,
                                          "region": {"x": 0, "y": 0,
                                                     "width": 200, "height": 60}})
            check("window + region is window-relative",
                  strip.get("region", {}).get("x") == wx
                  and strip.get("region", {}).get("y") == wy,
                  f'region={strip.get("region")} window at ({wx}, {wy})')

            # -- scale above 1 enlarges a small crop --------------------------
            tiny = s.call("screenshot", {"region": {"x": wx, "y": wy + 100,
                                                    "width": 120, "height": 60},
                                         "scale": 3.0})
            check("scale above 1 enlarges a tiny crop",
                  (tiny.get("shown") or {}).get("dimensions", "") == "360x180",
                  f'shown={(tiny.get("shown") or {}).get("dimensions")}')

            # -- a miss and a hit are different ------------------------------
            # Measured on this window: dead space moves 0 cells by more than
            # 60/255; the smallest real button press moves 22.
            #
            # Clicked TWICE, and only the second click is asserted on. The
            # display area is inert on the second visit but not the first: the
            # first click into it places a caret and draws a focus ring, which
            # measured 261 strong cells and is a real change, correctly
            # reported. A fixture that calls that a miss would be testing the
            # wrong thing.
            s.call("pointer_click", {"x": wx + 180, "y": wy + 150,
                                     "expect_window": wid, "look": False})
            time.sleep(0.5)
            miss = s.call("pointer_click", {"x": wx + 180, "y": wy + 150,
                                            "expect_window": wid})
            miss_look = miss.get("look") or {}
            check("a click into dead space reports a miss",
                  "NOTHING" in (miss_look.get("verdict") or ""),
                  f'changed={miss_look.get("changed")}')
            check("a miss spends no tokens on a picture",
                  "image" not in miss["_blocks"],
                  f'blocks={miss["_blocks"]}')

            hit = s.call("pointer_click", {"x": wx + 44, "y": wy + 438,
                                           "expect_window": wid})
            hit_look = hit.get("look") or {}
            check("a real button press reports a hit",
                  "landed" in (hit_look.get("verdict") or ""),
                  f'changed={hit_look.get("changed")}')
            check("a hit comes back with the picture",
                  "image" in hit["_blocks"],
                  f'blocks={hit["_blocks"]}')
            check("the look is scoped to the window that was hit",
                  hit_look.get("of") == win["wm_class"],
                  f'of={hit_look.get("of")}')

            # -- look:false is genuinely free ---------------------------------
            quiet = s.call("pointer_click", {"x": wx + 44, "y": wy + 438,
                                             "expect_window": wid, "look": False})
            check("look:false attaches nothing",
                  "image" not in quiet["_blocks"] and "look" not in quiet,
                  f'blocks={quiet["_blocks"]}')

            # -- several actions, one round trip ------------------------------
            batch = s.call("do_steps", {"steps": [
                {"do": "activate", "target": wid},
                {"do": "click", "x": wx + 44, "y": wy + 390},
                {"do": "click", "x": wx + 44, "y": wy + 438},
                {"do": "sleep", "ms": 100},
            ]})
            check("do_steps runs every step and looks once",
                  batch.get("all_ok") and batch.get("steps_run") == 4
                  and batch["_images"] == 1,
                  f'ok={batch.get("all_ok")} run={batch.get("steps_run")} '
                  f'images={batch["_images"]}')

            # look:false has exactly one job, and a re-scoping branch used to
            # override it whenever the sequence ended on a different window
            # than it started on -- which "activate then click" always does.
            silent = s.call("do_steps", {"look": False, "steps": [
                {"do": "activate", "target": wid},
                {"do": "click", "x": wx + 44, "y": wy + 438},
            ]})
            check("do_steps look:false attaches nothing",
                  silent["_images"] == 0 and "look" not in silent,
                  f'blocks={silent["_blocks"]}')

            # A sequence made only of ui_press steps names no window and no
            # point. It used to fall back to a whole-screen look, where a
            # calculator's answer is 8 cells of 1920x1080 and anything else
            # moving on screen buries it -- six presses that all worked,
            # reported as "nothing changed".
            buttons = s.call("ui_find", {"app": "gnome-calculator",
                                         "role": "button"})
            by_name = {r["name"]: r["path"] for r in buttons.get("results") or []}
            if {"C", "7", "="} <= set(by_name):
                pressed = s.call("do_steps", {"steps": [
                    {"do": "press", "path": by_name["C"], "expect_name": "C"},
                    {"do": "press", "path": by_name["7"], "expect_name": "7"},
                    {"do": "press", "path": by_name["="], "expect_name": "="},
                ]})
                pressed_look = pressed.get("look") or {}
                check("do_steps of ui_press steps finds its own window",
                      pressed.get("all_ok")
                      and pressed_look.get("of") == win["wm_class"],
                      f'ok={pressed.get("all_ok")} of={pressed_look.get("of")}')

            bad = s.call("do_steps", {"steps": [
                {"do": "activate", "target": wid},
                {"do": "key", "target": wid, "combo": "ctrl+alt+f2"},
                {"do": "click", "x": wx + 44, "y": wy + 438},
            ]})
            check("do_steps stops at a failing step and shows the state",
                  bad.get("failed_at_step") == 1 and bad.get("steps_run") == 2,
                  f'failed_at={bad.get("failed_at_step")} run={bad.get("steps_run")}')

            # -- reading the screen without an image --------------------------
            found = s.call("find_text", {"text": "mod", "window": wid})
            results = found.get("results") or []
            inside = bool(results) and (
                wx <= results[0]["click_at"][0] <= wx + win["width"]
                and wy <= results[0]["click_at"][1] <= wy + win["height"])
            check("find_text locates visible text in screen coordinates",
                  found.get("matches", 0) >= 1 and inside,
                  f'matches={found.get("matches")} at={results[0]["click_at"] if results else None}')
            check("find_text costs no image",
                  "image" not in found["_blocks"], f'blocks={found["_blocks"]}')

            # -- waiting on pixels -------------------------------------------
            quiet_wait = s.call("region_changed", {"window": wid, "timeout": 2,
                                                   "look": False})
            check("region_changed does not fire on a still screen",
                  quiet_wait.get("changed") is False,
                  f'largest={quiet_wait.get("largest_change_seen")}')

            # -- ui_find survives an app it cannot read -----------------------
            wide = s.call("ui_find", {"text": "Calculator"})
            check("ui_find still answers when one app refuses to be read",
                  wide.get("matches", 0) >= 1,
                  f'matches={wide.get("matches")} '
                  f'unreachable={len(wide.get("unreachable_apps") or [])}')
            listed = s.call("ui_find", {"app": "gnome-calculator",
                                        "actionable_only": True})
            check("ui_find can list actionable widgets with no text",
                  listed.get("matches", 0) > 10, f'matches={listed.get("matches")}')

            # -- the key tables agree ----------------------------------------
            digit = s.call("press_keys", {"target": wid, "combo": "7", "look": False})
            check("press_keys knows about digits",
                  not digit["_is_error"], digit.get("detail") or digit["_text"][:80])
    finally:
        launched.terminate()
        try:
            launched.wait(timeout=5)
        except subprocess.TimeoutExpired:                    # pragma: no cover
            launched.kill()

    width = max(len(c[0]) for c in CHECKS)
    for label, ok, detail in CHECKS:
        print(f'{"PASS" if ok else "FAIL"}  {label:<{width}}  {detail}')
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
