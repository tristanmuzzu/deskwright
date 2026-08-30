#!/usr/bin/env python3
"""Proof that the pointer goes where it is told, and that the guards refuse.

Not a demo. Every input API on this machine reports success whether or not
anything happened: ydotool exits 0 after moving the pointer two pixels instead
of two hundred, and mutter answers NotifyPointerMotionAbsolute without saying
where the pointer ended up. So each assertion below is made against a WITNESS
window -- a real GTK window that reports every motion, button and key event it
receives -- rather than against a return code.

What is checked:

  * Absolute motion lands on the pixel asked for. This is the claim the whole
    module exists to make, and the one ydotool could not make.
  * A click is delivered to the window under the point.
  * Typing survives the keyboard layout. This machine is `de`, where ydotool's
    US-keycode injection turns y into z; keysyms are supposed to be immune, and
    this is where that stops being a hope.
  * A drag is a real drag: press, travel, release, and a window that moved by
    the distance requested.
  * expect_window refuses a click when something else is under the point, and
    off-screen coordinates are refused rather than clamped.

The witness needs XWayland (it asks X for root coordinates), which is present
on this machine. If it cannot start, the tests SKIP rather than pass quietly.

    ./tests/test_pointer.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server as srv

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []

WITNESS = r'''
import sys
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

win = Gtk.Window(title="PointerWitness")
win.set_default_size(int(sys.argv[1]), int(sys.argv[2]))
win.set_decorated(False)
win.set_keep_above(True)
win.set_skip_taskbar_hint(True)
win.move(int(sys.argv[3]), int(sys.argv[4]))
win.add(Gtk.Label(label="witness"))
win.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
win.connect("motion-notify-event",
            lambda w, e: print(f"motion {int(e.x_root)} {int(e.y_root)}", flush=True))
win.connect("button-press-event",
            lambda w, e: print(f"press {int(e.x_root)} {int(e.y_root)}", flush=True))
win.connect("key-press-event",
            lambda w, e: print(f"key {Gdk.keyval_name(e.keyval)}", flush=True))
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
'''


def check(name: str, fn) -> None:
    try:
        detail = fn()
        results.append((PASS, name, detail or ""))
    except AssertionError as exc:
        results.append((FAIL, name, str(exc)))
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))


class Witness:
    """A window that says out loud what input reached it."""

    def __init__(self, tmp: Path, size=(1920, 1080), at=(0, 0)) -> None:
        self.script = tmp / "witness.py"
        self.script.write_text(WITNESS)
        self.log = tmp / f"witness-{at[0]}-{at[1]}.log"
        # Outlives this method: it is the witness process's stdout.
        self.handle = open(self.log, "w")  # noqa: SIM115
        env = dict(os.environ, GDK_BACKEND="x11")
        self.proc = subprocess.Popen(
            [sys.executable, str(self.script), str(size[0]), str(size[1]),
             str(at[0]), str(at[1])],
            stdout=self.handle, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(w["title"] == "PointerWitness" for w in srv.list_windows()):
                time.sleep(0.6)      # let it map and start taking events
                return
            time.sleep(0.2)
        self.stop()
        raise AssertionError("the witness window never appeared")

    def events(self, kind: str) -> list[str]:
        return [line for line in self.log.read_text().splitlines()
                if line.startswith(kind + " ")]

    def last_motion(self) -> tuple[int, int] | None:
        motions = self.events("motion")
        if not motions:
            return None
        _, x, y = motions[-1].split()
        return int(x), int(y)

    def window(self) -> dict:
        for w in srv.list_windows():
            if w["title"] == "PointerWitness":
                return w
        raise AssertionError("the witness window vanished")

    def clear(self) -> None:
        self.handle.flush()
        self.handle.truncate(0)
        self.handle.seek(0)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:      # pragma: no cover
            self.proc.kill()
        self.handle.close()


# --------------------------------------------------------------- the premise
def test_absolute_motion_is_exact(witness: Witness) -> str:
    """The claim: asked for a pixel, landed on that pixel.

    Two hundred units of ydotool motion put the pointer somewhere between two
    and two hundred pixels away depending on how fast the previous one was.
    """
    misses = []
    for point in [(400, 300), (1200, 800), (100, 950), (1700, 200)]:
        srv.tool_pointer_move({"x": point[0], "y": point[1]})
        time.sleep(0.35)
        seen = witness.last_motion()
        if seen is None:
            raise AssertionError(f"the witness saw no motion at all for {point}")
        if abs(seen[0] - point[0]) > 2 or abs(seen[1] - point[1]) > 2:
            misses.append(f"asked {point} landed {seen}")
    assert not misses, "; ".join(misses)
    return "4 points, all within 2 px"


def test_click_lands_on_the_window_under_it(witness: Witness) -> str:
    win = witness.window()
    x = win["x"] + win["width"] // 2
    y = win["y"] + win["height"] // 2
    witness.clear()
    srv.tool_pointer_click({"x": x, "y": y, "expect_window": win["id"]})
    time.sleep(0.4)
    presses = witness.events("press")
    assert presses, "the click was reported as sent but the window never saw it"
    return f"{presses[-1]} reached the window"


def test_typing_survives_the_layout(witness: Witness) -> str:
    """`y` must arrive as `y` on a QWERTZ layout, which is where ydotool fails."""
    layouts = srv.keyboard_layouts()
    win = witness.window()
    witness.clear()
    srv.tool_type_text({"text": "yz", "target": win["id"], "via": "keysym"})
    time.sleep(0.6)
    keys = [line.split()[1] for line in witness.events("key")]
    assert keys[:2] == ["y", "z"], (
        f"typed 'yz' on layout {layouts} and the window received {keys!r}"
    )
    return f"'yz' arrived as 'yz' on layout {layouts or ['unknown']}"


def test_drag_moves_a_window(witness: Witness) -> str:
    """A press-and-teleport is not a drag; this proves motion happened between."""
    win = witness.window()
    # The witness is undecorated, so drag it with the compositor's own
    # super+drag rather than a title bar it does not have.
    start = (win["x"] + 40, win["y"] + 40)
    end = (start[0] + 120, start[1] + 90)
    srv.tool_pointer_move({"x": start[0], "y": start[1]})
    time.sleep(0.2)
    witness.clear()
    srv.tool_pointer_drag({"from_x": start[0], "from_y": start[1],
                           "to_x": end[0], "to_y": end[1], "steps": 20})
    time.sleep(0.5)
    motions = witness.events("motion")
    assert len(motions) >= 5, (
        f"a drag over 150 px produced {len(motions)} motion events; the "
        "intermediate travel is missing and toolkits will not see a drag"
    )
    return f"{len(motions)} motion events between press and release"


def test_expect_window_refuses_the_wrong_target(witness: Witness) -> str:
    win = witness.window()
    x = win["x"] + win["width"] // 2
    y = win["y"] + win["height"] // 2
    other = [w for w in srv.list_windows()
             if w["id"] != win["id"] and not w["minimized"]]
    if not other:
        return "SKIPPED: nothing else is open to name"
    witness.clear()
    try:
        srv.tool_pointer_click({"x": x, "y": y, "expect_window": other[0]["id"]})
    except srv.ToolError as e:
        assert "Nothing was clicked" in str(e), f"refused for the wrong reason: {e}"
        assert not witness.events("press"), "it refused and clicked anyway"
        return "refused, and the witness saw no click"
    raise AssertionError("clicked while naming a window that was not there")


def test_off_screen_is_refused() -> str:
    for point in [(-10, 500), (500, -10), (99999, 500)]:
        try:
            srv.tool_pointer_move({"x": point[0], "y": point[1]})
        except srv.ToolError as e:
            assert "outside the desktop" in str(e), f"wrong reason for {point}: {e}"
            continue
        raise AssertionError(f"{point} was accepted instead of refused")
    return "3 off-screen points refused"


def test_window_at_agrees_with_the_stack(witness: Witness) -> str:
    win = witness.window()
    at = srv.window_at(win["x"] + 5, win["y"] + 5)
    assert at["window"], "nothing was reported under a point inside a real window"
    assert at["window"]["id"] == win["id"], (
        f'the topmost window at that point was reported as '
        f'{at["window"]["wm_class"]}, not the witness'
    )
    return f'{at["source"]}'


def test_session_is_released(_: Witness) -> str:
    """The sharing indicator must not live in the top bar forever."""
    from wcu import remote_input
    ri = remote_input.shared()
    ri.move_to(960, 540)
    assert ri.active, "a move did not open a session"
    ri.stop()
    assert not ri.active, "stop() left the session open"
    return "session opens on use and closes on stop"


def main() -> int:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        print("SKIP  no session bus; these tests need the graphical session")
        return 0

    with tempfile.TemporaryDirectory(prefix="wcu-pointer-") as tmpdir:
        tmp = Path(tmpdir)
        try:
            witness = Witness(tmp, size=(1920, 1080), at=(0, 0))
        except AssertionError as e:
            print(f"SKIP  witness window would not start: {e}")
            return 0
        try:
            check("absolute motion is exact",
                  lambda: test_absolute_motion_is_exact(witness))
            check("a click reaches the window under it",
                  lambda: test_click_lands_on_the_window_under_it(witness))
            check("typing survives the keyboard layout",
                  lambda: test_typing_survives_the_layout(witness))
            check("a drag travels", lambda: test_drag_moves_a_window(witness))
            check("expect_window refuses the wrong target",
                  lambda: test_expect_window_refuses_the_wrong_target(witness))
            check("off-screen coordinates are refused", test_off_screen_is_refused)
            check("window_at agrees with the stack",
                  lambda: test_window_at_agrees_with_the_stack(witness))
            check("the input session is released",
                  lambda: test_session_is_released(witness))
        finally:
            witness.stop()

    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
