#!/usr/bin/env python3
"""Record the screen, or one window, to an mp4 -- no portal consent dialog.

WHY THIS EXISTS

`screenshot` gives an agent a still. Anything that MOVES -- an animation, a
scroll, a hover state, a stutter -- is invisible in stills, and bursting
screenshots tops out around 5 fps with a visible tear between frames.

WHY IT DOES NOT USE THE PORTAL

`org.freedesktop.portal.ScreenCast` is the documented route and it pops a
"share your screen?" dialog every single run, which makes it useless to an
agent working while Tristan is doing something else. `org.gnome.Mutter.ScreenCast`
is the layer underneath it and, on this machine, answers CreateSession for an
ordinary session-bus client. Verified, not assumed: see tests/test_screencast.py.

THE ONE NON-OBVIOUS CONSTRAINT

Mutter ties the session's lifetime to the D-Bus *connection* that created it.
The moment that connection drops, the session is destroyed -- which is why this
cannot be a shell one-liner and has to be a process that stays alive for the
whole recording.

USAGE

    screencast out.mp4                       # whole screen, 10s, 30fps
    screencast --seconds 5 out.mp4
    screencast --window 72777864 out.mp4     # one window, by id from `windows`
    screencast --window chrome out.mp4       # ...or by wm_class / title fragment
    screencast --fps 60 --cursor out.mp4

Prints one JSON object on stdout: path, seconds, fps, size, and what was captured.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS = "org.gnome.Mutter.ScreenCast"
PATH = "/org/gnome/Mutter/ScreenCast"
DISPLAY_BUS = "org.gnome.Mutter.DisplayConfig"
DISPLAY_PATH = "/org/gnome/Mutter/DisplayConfig"
WINDOWS_HELPER = os.path.expanduser("~/.claude/bin/windows")

# cursor-mode: 0 hidden, 1 embedded in the frames, 2 sent as separate metadata.
CURSOR_HIDDEN, CURSOR_EMBEDDED = 0, 1


class Failed(Exception):
    pass


def _proxy(conn: Gio.DBusConnection, bus: str, path: str, iface: str) -> Gio.DBusProxy:
    return Gio.DBusProxy.new_sync(
        conn, Gio.DBusProxyFlags.NONE, None, bus, path, iface, None
    )


def primary_connector(conn: Gio.DBusConnection) -> str:
    """The connector name Mutter wants for RecordMonitor, e.g. 'eDP-1'."""
    state = _proxy(conn, DISPLAY_BUS, DISPLAY_PATH, DISPLAY_BUS).call_sync(
        "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None
    )
    monitors = state[1]
    if not monitors:
        raise Failed("Mutter reports no monitors, so there is nothing to record")
    for (connector, *_), modes, _props in monitors:
        # A mode is (id, w, h, refresh, preferred-scale, [scales], {properties}).
        # Index by "the trailing dict" rather than a number so a future Mutter
        # adding a field does not silently pick the wrong monitor.
        for mode in modes:
            props = next((f for f in reversed(mode) if isinstance(f, dict)), {})
            if props.get("is-current"):
                return connector
    return monitors[0][0][0]


def resolve_window(target: str) -> dict:
    """Reuse the `windows` helper so id/wm_class/title matching stays in one place."""
    if not os.access(WINDOWS_HELPER, os.X_OK):
        raise Failed(f"{WINDOWS_HELPER} is missing, so --window cannot resolve names")
    windows = json.loads(subprocess.run(
        [WINDOWS_HELPER], capture_output=True, text=True, check=True
    ).stdout)
    if target.isdigit():
        for w in windows:
            if w["id"] == int(target):
                return w
        raise Failed(f"no window with id {target}")
    needle = target.lower()
    matches = [w for w in windows
               if needle in (w.get("wm_class") or "").lower()
               or needle in (w.get("title") or "").lower()]
    if not matches:
        listing = ", ".join(f'{w["id"]} ({w["wm_class"]})' for w in windows)
        raise Failed(f"no window matching {target!r}. Open: {listing}")
    if len(matches) > 1:
        listing = ", ".join(f'{w["id"]} ({w["wm_class"]}: {w["title"][:40]})'
                            for w in matches)
        raise Failed(f"{target!r} matches {len(matches)} windows, so pass an id: {listing}")
    return matches[0]


def encoder_chain() -> tuple[str, str]:
    """Prefer the iGPU. Falls back to x264 on CPU, which at 1080p60 is not free."""
    probe = subprocess.run(["gst-inspect-1.0", "vah264enc"],
                           capture_output=True, text=True, check=False)
    if probe.returncode == 0 and os.path.exists("/dev/dri/renderD128"):
        return "vah264enc", "vapostproc ! video/x-raw(memory:VAMemory) ! vah264enc"
    return "x264enc", ("videoconvert ! x264enc speed-preset=veryfast "
                       "tune=zerolatency key-int-max=60")


def record(args: argparse.Namespace) -> dict:
    for tool in ("gst-launch-1.0",):
        if not shutil.which(tool):
            raise Failed(f"{tool} is not installed (apt install gstreamer1.0-tools)")

    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    screencast = _proxy(conn, BUS, PATH, BUS)

    session_path = screencast.call_sync(
        "CreateSession", GLib.Variant("(a{sv})", ({},)),
        Gio.DBusCallFlags.NONE, -1, None
    )[0]
    session = _proxy(conn, BUS, session_path, f"{BUS}.Session")

    cursor = CURSOR_EMBEDDED if args.cursor else CURSOR_HIDDEN
    opts = {"cursor-mode": GLib.Variant("u", cursor)}

    if args.window:
        win = resolve_window(args.window)
        opts["window-id"] = GLib.Variant("t", win["id"])
        stream_path = session.call_sync(
            "RecordWindow", GLib.Variant("(a{sv})", (opts,)),
            Gio.DBusCallFlags.NONE, -1, None
        )[0]
        captured = f'window {win["id"]} ({win["wm_class"]}: {win["title"][:60]})'
    else:
        connector = primary_connector(conn)
        stream_path = session.call_sync(
            "RecordMonitor", GLib.Variant("(sa{sv})", (connector, opts)),
            Gio.DBusCallFlags.NONE, -1, None
        )[0]
        captured = f"monitor {connector}"

    # The node id only exists once Mutter has actually built the stream, and it
    # arrives as a signal rather than a return value, so Start() has to happen
    # with the handler already attached.
    loop = GLib.MainLoop()
    state: dict = {}

    def on_stream_added(_conn, _sender, _path, _iface, _signal, params):
        state["node_id"] = params[0]
        loop.quit()

    sub = conn.signal_subscribe(
        None, f"{BUS}.Stream", "PipeWireStreamAdded", stream_path, None,
        Gio.DBusSignalFlags.NONE, on_stream_added
    )
    GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
    session.call_sync("Start", None, Gio.DBusCallFlags.NONE, -1, None)
    loop.run()
    conn.signal_unsubscribe(sub)

    if "node_id" not in state:
        session.call_sync("Stop", None, Gio.DBusCallFlags.NONE, -1, None)
        raise Failed("Mutter accepted the session but never published a PipeWire "
                     "node, so there is nothing to encode")

    encoder, chain = encoder_chain()
    pipeline = [
        "gst-launch-1.0", "-q", "-e",
        "pipewiresrc", f"path={state['node_id']}", "do-timestamp=true", "!",
        "videorate", "!", f"video/x-raw,framerate={args.fps}/1", "!",
        *chain.split(), "!",
        "h264parse", "!", "mp4mux", "!", "filesink", f"location={args.output}",
    ]
    # gst-launch needs the whole graph as one argv; the '!' tokens above are
    # already separate arguments, which is what it expects.
    started = time.time()
    gst = subprocess.Popen(pipeline, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
    try:
        time.sleep(args.seconds)
    finally:
        # -e plus SIGINT is the only way gst finalises the mp4 moov atom; SIGKILL
        # leaves a file that no player will open.
        gst.send_signal(signal.SIGINT)
        try:
            gst.wait(timeout=15)
        except subprocess.TimeoutExpired:
            gst.kill()
            gst.wait()
        elapsed = time.time() - started
        session.call_sync("Stop", None, Gio.DBusCallFlags.NONE, -1, None)

    stderr = gst.stderr.read().decode(errors="replace").strip() if gst.stderr else ""
    if not os.path.exists(args.output) or os.path.getsize(args.output) == 0:
        raise Failed(f"gst-launch produced no output. {stderr[:400]}")

    return {
        "path": os.path.abspath(args.output),
        "captured": captured,
        "seconds": round(elapsed, 1),
        "fps": args.fps,
        "encoder": encoder,
        "bytes": os.path.getsize(args.output),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("output", help="output path, .mp4")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--window", help="window id, wm_class, or title fragment")
    p.add_argument("--cursor", action="store_true", help="burn the pointer in")
    args = p.parse_args()

    if not args.output.endswith(".mp4"):
        print(json.dumps({"error": "output must end in .mp4"}), file=sys.stderr)
        return 2
    try:
        print(json.dumps(record(args), indent=1))
    except Failed as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
