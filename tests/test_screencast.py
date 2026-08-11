#!/usr/bin/env python3
"""Proof that screen recording actually records this screen.

Not a demo. `gst-launch-1.0` exits 0 on a pipeline that produced a valid-looking
mp4 full of black frames, and Mutter answers CreateSession happily even when it
will never publish a PipeWire node -- so "the call succeeded" proves nothing
here. Every assertion below looks at the FILE afterwards.

What is checked:

  * The premise. `org.gnome.Mutter.ScreenCast` answers an ordinary session-bus
    client on this machine. That is the whole reason this bypasses the xdg
    portal's consent dialog, and it is a compositor-version-dependent fact, so
    it gets asserted rather than assumed. If a future GNOME locks it down, this
    is the test that says so instead of a mysterious empty file.
  * Session lifetime. Mutter destroys the session when the creating D-Bus
    connection drops. This is the constraint that forces screencast.py to be a
    long-lived process, so it is pinned here.
  * The recording is real: h264, right duration, frame count in the right
    neighbourhood, and -- the one that catches black frames -- non-trivial
    pixel variance in a frame pulled back out with ffmpeg.
  * Window capture resolves a target and records that window.
  * The guards reject bad input rather than writing junk.

    ./tests/test_screencast.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server as srv

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        results.append((PASS, name, detail or ""))
    except AssertionError as exc:
        results.append((FAIL, name, str(exc)))
    except Exception as exc:  # noqa: BLE001 - a test file wants the reason, not a trace
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))


def ffprobe(path: Path, entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries,
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


# ---------------------------------------------------------------- the premise
def test_mutter_answers_an_ordinary_client() -> str:
    """If this fails, everything else here is unreachable and the portal is the
    only route left -- which means a consent dialog on every recording."""
    out = subprocess.run(
        ["gdbus", "call", "--session",
         "--dest", "org.gnome.Mutter.ScreenCast",
         "--object-path", "/org/gnome/Mutter/ScreenCast",
         "--method", "org.gnome.Mutter.ScreenCast.CreateSession", "{}"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, (
        "Mutter refused CreateSession to an ordinary client: "
        f"{out.stderr.strip()[:200]}. The portal route is the fallback."
    )
    assert "/org/gnome/Mutter/ScreenCast/Session/" in out.stdout, out.stdout
    return out.stdout.strip()


def test_session_dies_with_its_connection() -> str:
    """Pinning the constraint that forces screencast.py to stay resident."""
    created = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.ScreenCast",
         "--object-path", "/org/gnome/Mutter/ScreenCast",
         "--method", "org.gnome.Mutter.ScreenCast.CreateSession", "{}"],
        capture_output=True, text=True, check=True,
    ).stdout
    path = created.split("'")[1]
    # That gdbus process is gone now, so its session should be too.
    probe = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.ScreenCast",
         "--object-path", path,
         "--method", "org.gnome.Mutter.ScreenCast.Session.Stop"],
        capture_output=True, text=True, check=False,
    )
    assert probe.returncode != 0, (
        f"{path} outlived the connection that made it -- the long-lived-process "
        "design in screencast.py may no longer be necessary"
    )
    return "session vanished with its connection, as designed around"


# ------------------------------------------------------------- real recording
def test_full_screen_recording_has_moving_content(tmp: Path) -> str:
    out = srv.tool_screencast({"path": str(tmp / "screen.mp4"), "seconds": 3, "fps": 30})
    path = Path(out["path"])
    assert path.exists() and path.stat().st_size > 50_000, out

    streams = ffprobe(path, "stream=codec_name,width,height,nb_frames")["streams"]
    assert streams, "no video stream in the file"
    s = streams[0]
    assert s["codec_name"] == "h264", s
    assert int(s["width"]) >= 640 and int(s["height"]) >= 480, s
    frames = int(s.get("nb_frames", 0))
    assert 40 <= frames <= 130, f"3s at 30fps should be ~90 frames, got {frames}"

    # The black-frame check. A recording of a real desktop has spread; a black
    # or single-colour frame has a standard deviation near zero.
    frame = tmp / "probe.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                    "-vf", "select=eq(n\\,20)", "-vframes", "1", str(frame)],
                   check=True)
    stats = subprocess.run(
        ["magick", str(frame), "-format", "%[fx:standard_deviation]", "info:"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert float(stats) > 0.02, (
        f"frame is essentially flat (stddev {stats}) -- recorded black, not the screen"
    )
    return f'{s["width"]}x{s["height"]} {frames}f stddev={stats} via {out["encoder"]}'


def test_window_recording_targets_that_window(tmp: Path) -> str:
    windows = [w for w in srv.list_windows() if w["wm_class"] and not w["minimized"]]
    assert windows, "no window is open to record"
    target = windows[0]
    out = srv.tool_screencast({"path": str(tmp / "win.mp4"), "seconds": 2,
                               "target": target["id"]})
    assert str(target["id"]) in out["captured"], out
    assert Path(out["path"]).stat().st_size > 20_000, out
    return out["captured"][:80]


# -------------------------------------------------------------------- guards
def test_guards(tmp: Path) -> str:
    cases = [
        ({"path": str(tmp / "x.png")}, "must end in .mp4"),
        ({"path": str(tmp / "y.mp4"), "seconds": 999}, "seconds must be"),
        ({"path": str(tmp), "seconds": 1}, "is a directory"),
        ({"path": str(tmp / "z.mp4"), "target": "no-such-window-xyzzy"}, "nothing matches"),
    ]
    for args, expected in cases:
        try:
            srv.tool_screencast(args)
        except srv.ToolError as exc:
            assert expected in str(exc), f"{args} raised {exc!r}, wanted {expected!r}"
        else:
            raise AssertionError(f"{args} was accepted but should have been rejected")
    return f"{len(cases)} bad inputs rejected"


def main() -> int:
    for tool in ("ffprobe", "ffmpeg", "magick", "gst-launch-1.0"):
        if subprocess.run(["which", tool], capture_output=True,
                          check=False).returncode != 0:
            print(f"{SKIP}: {tool} is not installed; this test cannot verify anything")
            return 0

    check("mutter answers an ordinary client", test_mutter_answers_an_ordinary_client)
    check("session dies with its connection", test_session_dies_with_its_connection)

    with tempfile.TemporaryDirectory(prefix="wcu-screencast-") as td:
        tmp = Path(td)
        check("full-screen recording has moving content",
              lambda: test_full_screen_recording_has_moving_content(tmp))
        check("window recording targets that window",
              lambda: test_window_recording_targets_that_window(tmp))
        check("guards reject bad input", lambda: test_guards(tmp))

    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
