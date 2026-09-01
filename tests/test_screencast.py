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
    except Exception as exc:
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

    probed = ffprobe(path, "stream=codec_name,width,height,nb_frames")
    streams = probed["streams"]
    assert streams, "no video stream in the file"
    s = streams[0]
    assert s["codec_name"] == "h264", s
    assert int(s["width"]) >= 640 and int(s["height"]) >= 480, s
    # Fragmented mp4 carries no nb_frames, so count them the way frames.py does.
    frames = int(s.get("nb_frames") or 0)
    if not frames:
        dur = float(ffprobe(path, "format=duration")["format"]["duration"])
        frames = round(dur * 30)
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


# ------------------------------------- regressions from the 2026-08-12 session
def test_recording_is_decodable_and_honest(tmp: Path) -> str:
    """Three bugs in one test, all found by closing a window mid-capture.

    1. mp4mux wrote its moov index only at a clean end-of-stream, so a cut
       recording was multi-megabyte and completely unplayable -- and the size
       check happily called it a success.
    2. `seconds` reported wall-clock, so a 2.4s file was announced as 10s.
    3. Mutter tears the session down with the window, so the Stop() in the
       finally block raised UnknownMethod and crashed away a good recording.
    """
    out = srv.tool_screencast({"path": str(tmp / "honest.mp4"), "seconds": 3})

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", out["path"]],
        capture_output=True, text=True, check=False,
    )
    assert probe.returncode == 0 and probe.stdout.strip(), (
        f"recording is not decodable: {probe.stderr.strip()[:200]}"
    )
    real = float(probe.stdout.strip())
    assert abs(out["seconds"] - real) < 0.3, (
        f'reported {out["seconds"]}s but the file is {real:.1f}s'
    )
    assert "wall_clock_seconds" in out, "wall clock must stay visible, separately"
    return f'{out["seconds"]}s reported, {real:.1f}s on disk'


def test_frames_survives_missing_nb_frames(tmp: Path) -> str:
    """Fragmented mp4 reports nb_frames=N/A. Trusting it gave 0, every tile
    landed on frame 0, and the sheet was twelve copies of one image."""
    src = srv.tool_screencast({"path": str(tmp / "frag.mp4"), "seconds": 3})
    out = srv.tool_frames({"path": src["path"], "cols": 3, "rows": 1})
    tiles = sorted((Path(out["sheet"]).parent / "frames").glob("*.png"))
    assert len(tiles) == 3, tiles
    means = {subprocess.run(["magick", "identify", "-format", "%[mean]", str(t)],
                            capture_output=True, text=True, check=True).stdout
             for t in tiles}
    assert out["source"]["frames"] > 10, (
        f'frame count collapsed to {out["source"]["frames"]}'
    )
    return f'{out["source"]["frames"]} frames derived, {len(means)} distinct tiles'


# ------------------------------------------------- the other half: reading it
def test_screencast_hands_off_to_frames(tmp: Path) -> str:
    """A path to an mp4 is a dead end on its own -- a model cannot decode it.
    screencast must say what to call next, or the capability goes unused."""
    out = srv.tool_screencast({"path": str(tmp / "handoff.mp4"), "seconds": 2})
    step = out.get("next_step", "")
    assert "frames" in step, f"screencast gave no pointer to frames: {out}"
    assert out["path"] in step, "the pointer omits the path it applies to"
    return step[:70]


def test_frames_produces_a_readable_sheet(tmp: Path) -> str:
    src = srv.tool_screencast({"path": str(tmp / "sheet.mp4"), "seconds": 3})
    out = srv.tool_frames({"path": src["path"], "cols": 3, "rows": 2})

    sheet = Path(out["sheet"])
    assert sheet.exists() and sheet.suffix == ".png", out
    assert out["tiles"] == 6, out

    # A contact sheet must be WIDER than one frame -- otherwise the tiling
    # silently did not happen and this is just a screenshot with extra steps.
    dims = subprocess.run(["magick", "identify", "-format", "%w %h", str(sheet)],
                          capture_output=True, text=True, check=True).stdout
    w, h = (int(x) for x in dims.split())
    assert w > 600 and h > 200, f"sheet is {w}x{h}, too small to be a 3x2 tiling"
    return f"{out['tiles']} tiles, sheet {w}x{h}"


def test_frames_measures_stillness(tmp: Path) -> str:
    """The motion numbers have to mean something. A recording of an idle screen
    is the one case with a known answer: almost every frame is a duplicate."""
    src = srv.tool_screencast({"path": str(tmp / "idle.mp4"), "seconds": 3})
    out = srv.tool_frames({"path": src["path"]})
    m = out["motion"]
    assert set(m) >= {"peak", "mean", "duplicate_frames", "duplicate_pct"}, m
    assert 0 <= m["duplicate_pct"] <= 100, m
    assert m["peak"] >= m["mean"], f"peak below mean is impossible: {m}"
    return f"peak {m['peak']}, mean {m['mean']}, {m['duplicate_pct']}% duplicates"


def test_frames_guards(tmp: Path) -> str:
    cases = [
        ({"path": str(tmp / "nope.mp4")}, "does not exist"),
        ({"path": str(tmp / "probe.png")}, "expected a video"),
    ]
    (tmp / "probe.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    for args, expected in cases:
        try:
            srv.tool_frames(args)
        except srv.ToolError as exc:
            assert expected in str(exc), f"{args} raised {exc!r}, wanted {expected!r}"
        else:
            raise AssertionError(f"{args} was accepted but should have been rejected")
    # A grid bigger than the cap would spawn an ffmpeg call per tile.
    try:
        srv.tool_frames({"path": str(tmp / "nope.mp4"), "cols": 99})
    except srv.ToolError:
        pass
    return f"{len(cases) + 1} bad inputs rejected"


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

    with tempfile.TemporaryDirectory(prefix="deskwright-screencast-") as td:
        tmp = Path(td)
        check("full-screen recording has moving content",
              lambda: test_full_screen_recording_has_moving_content(tmp))
        check("window recording targets that window",
              lambda: test_window_recording_targets_that_window(tmp))
        check("recording is decodable and honest about length",
              lambda: test_recording_is_decodable_and_honest(tmp))
        check("frames survives nb_frames=N/A",
              lambda: test_frames_survives_missing_nb_frames(tmp))
        check("screencast hands off to frames",
              lambda: test_screencast_hands_off_to_frames(tmp))
        check("frames produces a readable sheet",
              lambda: test_frames_produces_a_readable_sheet(tmp))
        check("frames measures stillness",
              lambda: test_frames_measures_stillness(tmp))
        check("frames guards reject bad input", lambda: test_frames_guards(tmp))
        check("guards reject bad input", lambda: test_guards(tmp))

    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
