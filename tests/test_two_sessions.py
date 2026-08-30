#!/usr/bin/env python3
"""Two named headless desktops, driven at the same time, without seeing
each other.

Not a demo. The claim under test is ISOLATION, and isolation is exactly the
thing that looks fine right up until it is not: before names existed, two
agent sessions that both asked for a headless desktop silently got the SAME
one, and would have watched each other's windows move. Every assertion here
therefore checks the other session too.

What it proves:

  1. Two sessions come up under different names, with different buses,
     displays and runtime directories.
  2. Work done in one is INVISIBLE to the other: each sees only its own
     window, and each reads back only its own text.
  3. The user's primary session sees neither, and its accessibility bus
     still answers -- the 2026-08-24 incident was two sessions sharing one
     XDG_RUNTIME_DIR and breaking the user's real desktop.
  4. Stopping one leaves the other running.

Cost: two gnome-shell processes, ~300 MB each, for the length of the run.
The test refuses to start rather than push a loaded machine into swap.

    ./tests/test_two_sessions.py            # ~60s, needs ~700 MB free
    ./tests/test_two_sessions.py --keep     # leave both sessions up

Each session is driven in a SUBPROCESS, because pinning is per-process
environment: `pin_env()` mutates os.environ, so one interpreter can only
ever be pointed at one desktop at a time. That is not a limitation of the
design, it is the design -- but it does mean this test cannot be written
in a single process, and a version that tried would silently prove nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from wcu import headless

NAMES = ("wcutest-a", "wcutest-b")
passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {label:52} {detail}")
    else:
        failed += 1
        print(f"FAIL  {label:52} {detail}")


def drive(name: str, script: str) -> dict:
    """Run a snippet against one named session, in its own interpreter."""
    # A THROWAWAY XDG_DATA_HOME, and it is not optional.
    #
    # Only the SCREEN is separate on a headless session; the filesystem is
    # the user's real one. Launched bare, gnome-text-editor restored one of
    # HIS unsaved drafts -- a real .env -- and this test wrote its marker
    # into it (2026-08-27; the file on disk was never saved and the draft
    # cache was repaired, but that was luck, not design). An app started
    # with a fresh data home has no session to restore and opens empty.
    #
    # It must be set before launch_app, because the editor is spawned as a
    # child of this process and inherits this environment.
    prelude = textwrap.dedent(f"""
        import json, os, sys, tempfile
        sys.path.insert(0, {str(ROOT)!r})
        os.environ["WCU_SESSION"] = "headless:{name}"
        os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wcu-two-sessions-")
        import mcp_server              # pins this process at import
        out = {{}}
    """)
    # Dedent each piece on its own: concatenating an unindented line onto an
    # indented block leaves no common prefix, so a single dedent at the end
    # silently does nothing.
    body = "\n".join(textwrap.dedent(part) for part in script.split("\n@@\n"))
    proc = subprocess.run(
        [sys.executable, "-c", prelude + body
         + "\nprint('@@' + json.dumps(out))"],
        capture_output=True, text=True, timeout=180, cwd=ROOT)
    for line in proc.stdout.splitlines():
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise AssertionError(
        f"no result from the {name} session\n"
        f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}")


LAUNCH_AND_TYPE = """
    import time
    from wcu.atspi import (list_atspi_apps, tool_launch_app, tool_ui_read_text,
                           tool_ui_set_text)
    from wcu.shell import list_windows
    app = tool_launch_app({"desktop_id": "org.gnome.TextEditor",
                           "wait_window": True})
    out["window"] = app.get("window", {}).get("title")
    # A mapped window is not a registered accessible: the app appears on the
    # session's AT-SPI bus a moment after its window exists, and on a loaded
    # machine that moment is seconds. Poll instead of guessing a sleep.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(a["name"] == "gnome-text-editor" for a in list_atspi_apps()):
            break
        time.sleep(1.0)
    out["atspi_wait_s"] = round(30 - (deadline - time.monotonic()), 1)
    written = tool_ui_set_text({"app": "gnome-text-editor", "text": MINE})
    out["path"] = written.get("path")
    out["read_back"] = tool_ui_read_text({"path": out["path"]}).get("text", "")
    out["windows"] = [w["title"] for w in list_windows()]
    out["display"] = os.environ["WAYLAND_DISPLAY"]
    out["runtime_dir"] = os.environ["XDG_RUNTIME_DIR"]
"""

LOOK_ONLY = """
    from wcu.shell import list_windows
    from wcu.atspi import list_atspi_apps
    out["windows"] = [w["title"] for w in list_windows()]
    out["apps"] = sorted({a["name"] for a in list_atspi_apps()})
    out["display"] = os.environ["WAYLAND_DISPLAY"]
    out["runtime_dir"] = os.environ["XDG_RUNTIME_DIR"]
"""


def main() -> int:
    keep = "--keep" in sys.argv
    available = headless._available_mb()
    if available is not None and available < 700:
        print(f"SKIP: only {available} MB available; this test starts two "
              "compositors. Free some memory and re-run.")
        return 0

    # A session left over from an earlier run would make "it is running"
    # trivially true, so both names start from nothing.
    for name in NAMES:
        headless.stop(name)

    started = []
    try:
        for name in NAMES:
            report = headless.start(name=name)
            started.append(report)
            check(f"session {name!r} is up", report["running"],
                  f'display={report["wayland_display"]} '
                  f'rss={report.get("shell_rss_mb")}MB')

        a, b = started
        check("the two sessions have different buses",
              a["bus_address"] != b["bus_address"])
        check("...different wayland displays",
              a["wayland_display"] != b["wayland_display"],
              f'{a["wayland_display"]} vs {b["wayland_display"]}')
        check("...and different runtime dirs (the at-spi/bus collision)",
              a["runtime_dir"] != b["runtime_dir"],
              f'{Path(a["runtime_dir"]).name} vs {Path(b["runtime_dir"]).name}')

        listed = headless.list_sessions()
        live = {s["name"] for s in listed["sessions"] if s["running"]}
        check("both are listed as running at once", set(NAMES) <= live,
              f"running={sorted(live)} total_rss={listed['total_rss_mb']}MB")

        # Real work on both, one after the other, each in its own process.
        results = {}
        for name in NAMES:
            mine = f"written-into-{name}"
            results[name] = drive(name, f"MINE = {mine!r}\n@@\n" + LAUNCH_AND_TYPE)
            check(f"{name}: an app launched and took text",
                  mine in results[name]["read_back"],
                  f'read back {results[name]["read_back"][:40]!r}')
            # The editor must have opened EMPTY. If it restored one of the
            # user's own drafts, this test just wrote into a real document
            # of his -- which is what happened on 2026-08-27 before the
            # throwaway XDG_DATA_HOME above.
            body = results[name]["read_back"]
            check(f"{name}: the editor opened empty, not on the user's work",
                  body.strip() == mine,
                  "" if body.strip() == mine else
                  f"UNEXPECTED CONTENT -- {len(body)} chars, stop and check "
                  "~/.local/share/org.gnome.TextEditor/drafts/")

        # THE claim: neither session can see the other's work.
        for name, other in (NAMES, reversed(NAMES)):
            after = drive(name, LOOK_ONLY)
            others_text = f"written-into-{other}"
            check(f"{name} sees only its own window",
                  len(after["windows"]) == 1,
                  f'windows={after["windows"]}')
            check(f"{name} never sees {other}'s text",
                  all(others_text not in w for w in after["windows"]))

        # And the user's own desktop is untouched by either.
        from wcu.atspi import list_atspi_apps
        from wcu.shell import list_windows
        primary_titles = [w["title"] for w in list_windows()]
        check("the primary session sees neither editor",
              not any("written-into-" in t for t in primary_titles),
              f"{len(primary_titles)} windows open")
        check("the primary accessibility bus still answers",
              len(list_atspi_apps()) > 0,
              "the 2026-08-24 at-spi/bus collision has not recurred")

        if not keep:
            headless.stop(NAMES[0])
            check(f"stopping {NAMES[0]!r} leaves {NAMES[1]!r} running",
                  not headless.status(NAMES[0])["running"]
                  and headless.status(NAMES[1])["running"])
    finally:
        if keep:
            print(f"--keep: {' and '.join(NAMES)} left running")
        else:
            for name in NAMES:
                headless.stop(name)

    print(f"\n{passed}/{passed + failed} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
