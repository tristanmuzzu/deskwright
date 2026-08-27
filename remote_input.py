#!/usr/bin/env python3
"""Pointer and keyboard input that the compositor performs itself.

WHY THIS EXISTS

The input half of this project used to be ydotool, which writes evdev events
into /dev/uinput *below* the compositor. That layer cannot do the two things an
agent needs most:

  * Absolute pointing. `ydotool mousemove --absolute` does nothing on this
    machine, and relative motion is put through mutter's pointer acceleration
    curve on the way in, so units are not pixels and the ratio changes with
    speed. Measured 2026-08-16: 5 units moved 2 px, 200 units moved the pointer
    off the edge of the screen. An agent asked to "click that button" ended up
    writing a closed-loop controller that measured its own gain, and still
    clicked the wrong window twice.
  * Layout-proof text. ydotool speaks keycodes, which the compositor then maps
    through the active XKB layout, so on this machine's `de` layout every
    injected `y` arrives as `z` (see layout_hazard in mcp_server.py).

`org.gnome.Mutter.RemoteDesktop` is the API gnome-remote-desktop uses, it is on
the session bus, and mutter answers it for an ordinary session client with no
portal dialog -- the same bargain screencast.py already makes with
`org.gnome.Mutter.ScreenCast`. It offers absolute motion in a known coordinate
space, real button and axis events, and `NotifyKeyboardKeysym`, which is a
*character*, not a key position, so the layout cannot corrupt it.

Measured on GNOME 50.1 / Wayland, 2026-08-16: four absolute moves, four exact
landings (400,300 / 800,600 / 1600,900 / 100,100), confirmed by a window
reporting `x_root` for every motion event it saw.

THE TWO NON-OBVIOUS CONSTRAINTS

1. Mutter ties the session's life to the D-Bus *connection* that created it, so
   this module holds one connection for the life of the process. It does not
   need a subprocess the way screencast.py does, because it never blocks.
2. Absolute motion is expressed in the coordinate space of a screencast
   *stream*, so there has to be one, even though nothing ever consumes its
   frames. This records an AREA covering the whole logical desktop, which makes
   the stream's coordinates the same global logical coordinates that
   `list_windows` reports. Nothing connects to the PipeWire node, so no frames
   are ever produced.

A live session is what puts the "screen is being shared" indicator in the top
bar, so the session is created on first use and torn down again after
IDLE_TIMEOUT_S of quiet. The pointer stays where it was left.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

RD = "org.gnome.Mutter.RemoteDesktop"
RD_PATH = "/org/gnome/Mutter/RemoteDesktop"
RD_SESSION = f"{RD}.Session"
SC = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
SC_SESSION = f"{SC}.Session"
DISPLAY = "org.gnome.Mutter.DisplayConfig"
DISPLAY_PATH = "/org/gnome/Mutter/DisplayConfig"

# How long a session may sit idle before it is stopped. Long enough that a
# click-look-click sequence keeps one session, short enough that the sharing
# indicator does not live in the top bar all day.
IDLE_TIMEOUT_S = 25.0

# evdev button codes. Mutter wants these, not X11's 1/2/3.
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112,
           "back": 0x116, "forward": 0x115}

# X11 keysyms for the keys a combo can name. Keysyms are layout-independent:
# this is the whole reason for preferring them over evdev keycodes.
KEYSYMS = {
    "ctrl": 0xFFE3, "control": 0xFFE3, "leftctrl": 0xFFE3, "rightctrl": 0xFFE4,
    "shift": 0xFFE1, "leftshift": 0xFFE1, "rightshift": 0xFFE2,
    "alt": 0xFFE9, "leftalt": 0xFFE9, "rightalt": 0xFFEA, "altgr": 0xFFEA,
    "super": 0xFFEB, "meta": 0xFFEB, "win": 0xFFEB,
    "enter": 0xFF0D, "return": 0xFF0D, "esc": 0xFF1B, "escape": 0xFF1B,
    "tab": 0xFF09, "backspace": 0xFF08, "delete": 0xFFFF, "insert": 0xFF63,
    "space": 0x0020,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "menu": 0xFF67, "print": 0xFF61, "pause": 0xFF13,
    "capslock": 0xFFE5, "numlock": 0xFF7F, "scrolllock": 0xFF14,
}
KEYSYMS.update({f"f{n}": 0xFFBD + n for n in range(1, 13)})  # F1 = 0xFFBE
KEYSYMS.update({c: ord(c) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
KEYSYMS.update({
    "minus": 0x002D, "equal": 0x003D, "comma": 0x002C, "period": 0x002E,
    "slash": 0x002F, "semicolon": 0x003B, "apostrophe": 0x0027,
    "bracketleft": 0x005B, "bracketright": 0x005D, "backslash": 0x005C,
    "grave": 0x0060, "plus": 0x002B,
})


class InputError(Exception):
    """A failure the caller should see: no session, bad button, no monitors."""


def char_to_keysym(ch: str) -> int:
    """The X11 keysym for a character.

    Latin-1 is its own keysym range, and everything else is the codepoint with
    0x01000000 added -- the rule from the X protocol, which is what lets this
    type an em dash or an emoji without knowing anything about the layout.
    """
    code = ord(ch)
    if code == 0x0A or code == 0x0D:
        return 0xFF0D          # newline: press Return, not a control character
    if code == 0x09:
        return 0xFF09
    if 0x20 <= code <= 0xFF:
        return code
    return code | 0x01000000


class Gestures:
    """Click and drag built purely on move_to()/button(), shared by every
    input backend (mutter remote_input, portal portal_input).

    Lives here so a timing fix lands in all backends at once -- the staged
    drag below exists because the first drag implementation was duplicated
    nowhere but still wrong for a whole class of targets.
    """

    def click(self, x: float, y: float, button: str = "left", count: int = 1,
              settle: float = 0.06) -> None:
        self.move_to(x, y)
        # The pointer has to arrive before the button does, or the click is
        # delivered to whatever was under the old position.
        time.sleep(settle)
        for i in range(max(1, int(count))):
            if i:
                time.sleep(0.05)             # inside the double-click window
            self.button(button, True)
            time.sleep(0.02)
            self.button(button, False)

    def drag(self, x1: float, y1: float, x2: float, y2: float,
             button: str = "left", steps: int = 24,
             dwell_ms: int = 0) -> None:
        """Press at one point, travel, release at the other.

        The intermediate moves are not decoration: a press-and-teleport is not
        a drag to most toolkits, which start dragging on the first motion after
        the press and never see one.

        These timings are measured, not guessed, and were re-confirmed on
        2026-08-24 against a real browser drop target (a local dropzone page in
        Chrome on the headless session, page reloaded between trials so each
        one was independently measurable): **5/5 drops landed**. A "staged"
        variant written that day -- longer press, slow threshold escape, 450 ms
        hover before release -- scored **4/5** on the identical rig and is not
        in the tree. Cross-app drag-and-drop was listed as broken on
        2026-08-23 (Nautilus to WhatsApp Web); that report does not survive
        this measurement, and the likelier cause is an input grab held by
        something else (see the note in `_guard_point`) rather than these
        timings. Change them only against numbers from that rig.

        `dwell_ms` hovers at the destination before releasing, and defaults to
        0 BECAUSE of that measurement -- the default path is byte-for-byte the
        5/5 one. It exists for the case the rig did not cover: a cross-app,
        cross-toolkit drop (GTK source, Electron target) where the receiver
        runs its own dragover throttling and may not have painted a drop
        target yet when the button comes up. Reach for it only after a drop
        has actually failed; it is not a better default, it is a different
        bet, and the 4/5 result is what says so.
        """
        self.move_to(x1, y1)
        time.sleep(0.06)
        self.button(button, True)
        steps = max(2, int(steps))
        for i in range(1, steps + 1):
            self.move_to(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
            time.sleep(0.012)
        time.sleep(0.06)
        if dwell_ms > 0:
            # A second motion at the same point, then the wait: some receivers
            # only re-evaluate the drop target on a motion event, so a bare
            # sleep would hover without telling anyone.
            self.move_to(x2, y2)
            time.sleep(min(dwell_ms, 5000) / 1000.0)
        self.button(button, False)


class RemoteInput(Gestures):
    """One lazily-created mutter RemoteDesktop session, held open while in use."""

    def __init__(self, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
        self._idle_timeout = idle_timeout
        self._lock = threading.RLock()
        self._bus: Any = None
        self._rd_path: str | None = None
        self._sc_path: str | None = None
        self._stream: str | None = None
        self._area: tuple[int, int, int, int] | None = None
        self._timer: threading.Timer | None = None
        # Where we last put the pointer. Not the truth -- the human may have
        # moved the mouse since -- but the only answer available without the
        # shell extension, and honest about which it is.
        self.last_position: tuple[float, float] | None = None
        self.last_position_at: float = 0.0

    # ---------------------------------------------------------------- session
    def _connect(self) -> Any:
        if self._bus is None:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error as e:                       # pragma: no cover
                raise InputError(f"no session bus: {e.message}") from None
        return self._bus

    def _call(self, name: str, path: str, iface: str, method: str,
              args: Any = None, timeout: int = 10000) -> Any:
        bus = self._connect()
        try:
            return bus.call_sync(name, path, iface, method, args, None,
                                 Gio.DBusCallFlags.NONE, timeout, None)
        except GLib.Error as e:
            raise InputError(f"{method} failed: {e.message}") from None

    def desktop_bounds(self) -> tuple[int, int, int, int]:
        """The rectangle covering every monitor, in global logical pixels.

        This is the coordinate space of `list_windows`, and after `_ensure` it
        is also the coordinate space of every x/y this module takes.
        """
        state = self._call(DISPLAY, DISPLAY_PATH, DISPLAY, "GetCurrentState").unpack()
        monitors, logical = state[1], state[2]
        # Mode sizes live on the monitor, position and scale on the logical
        # monitor that shows it, so the two have to be joined on the connector.
        current_mode: dict[str, tuple[int, int]] = {}
        for monitor in monitors:
            connector = monitor[0][0]
            for mode in monitor[1]:
                if mode[6].get("is-current"):
                    current_mode[connector] = (mode[1], mode[2])
                    break
        left = top = 10**9
        right = bottom = -10**9
        for lm in logical:
            x, y, scale, transform = lm[0], lm[1], lm[2], lm[3]
            width = height = 0
            for spec in lm[5]:
                size = current_mode.get(spec[0])
                if size:
                    width, height = size
                    break
            if transform in (1, 3, 5, 7):   # 90/270 degrees: the mode is sideways
                width, height = height, width
            width = round(width / scale)
            height = round(height / scale)
            left, top = min(left, x), min(top, y)
            right, bottom = max(right, x + width), max(bottom, y + height)
        if right <= left or bottom <= top:
            raise InputError("mutter reported no usable monitors")
        return left, top, right - left, bottom - top

    def _ensure(self) -> tuple[str, str]:
        """A started session and a stream whose space is the whole desktop."""
        with self._lock:
            if self._rd_path and self._stream:
                self._touch()
                return self._rd_path, self._stream

            rd_path = self._call(RD, RD_PATH, RD, "CreateSession").unpack()[0]
            session_id = self._call(
                RD, rd_path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (RD_SESSION, "SessionId")),
            ).unpack()[0]
            sc_path = self._call(
                SC, SC_PATH, SC, "CreateSession",
                GLib.Variant("(a{sv})", ({
                    "remote-desktop-session-id": GLib.Variant("s", session_id),
                },)),
            ).unpack()[0]

            x, y, width, height = self.desktop_bounds()
            stream = self._call(
                SC, sc_path, SC_SESSION, "RecordArea",
                GLib.Variant("(iiiia{sv})", (x, y, width, height, {
                    # Nothing consumes this stream; the cursor would only cost
                    # composition work.
                    "cursor-mode": GLib.Variant("u", 0),
                })),
            ).unpack()[0]
            # Starting the remote-desktop session starts the screencast one it
            # owns. Starting the screencast session directly is refused
            # ("Must be started from remote desktop session").
            self._call(RD, rd_path, RD_SESSION, "Start")

            self._rd_path, self._sc_path, self._stream = rd_path, sc_path, stream
            self._area = (x, y, width, height)
            self._touch()
            return rd_path, stream

    def _touch(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle_timeout, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        """End the session. The pointer keeps the position it was left at."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._rd_path:
                try:
                    self._call(RD, self._rd_path, RD_SESSION, "Stop")
                except InputError:
                    pass                     # already gone; nothing to release
            self._rd_path = self._sc_path = self._stream = None

    @property
    def active(self) -> bool:
        return self._rd_path is not None

    # ---------------------------------------------------------------- pointer
    def _to_stream(self, x: float, y: float) -> tuple[float, float]:
        assert self._area is not None
        ax, ay, width, height = self._area
        sx, sy = x - ax, y - ay
        if not (0 <= sx <= width and 0 <= sy <= height):
            raise InputError(
                f"({x:.0f}, {y:.0f}) is outside the desktop, which is "
                f"{width}x{height} at ({ax}, {ay}). Nothing was moved."
            )
        return sx, sy

    def move_to(self, x: float, y: float) -> tuple[float, float]:
        rd_path, stream = self._ensure()
        sx, sy = self._to_stream(x, y)
        self._call(RD, rd_path, RD_SESSION, "NotifyPointerMotionAbsolute",
                   GLib.Variant("(sdd)", (stream, sx, sy)))
        self.last_position = (x, y)
        self.last_position_at = time.time()
        return x, y

    def button(self, name: str, pressed: bool) -> None:
        code = BUTTONS.get(str(name).lower())
        if code is None:
            raise InputError(
                f"unknown button {name!r}. Known: {', '.join(sorted(BUTTONS))}"
            )
        rd_path, _ = self._ensure()
        self._call(RD, rd_path, RD_SESSION, "NotifyPointerButton",
                   GLib.Variant("(ib)", (code, pressed)))

    def scroll(self, x: float, y: float, dy: int = 0, dx: int = 0) -> None:
        """Wheel clicks at a point. Positive dy scrolls down, dx scrolls right."""
        self.move_to(x, y)
        time.sleep(0.04)
        rd_path, _ = self._ensure()
        for axis, steps in ((0, int(dy)), (1, int(dx))):
            if not steps:
                continue
            for _ in range(abs(steps)):
                self._call(RD, rd_path, RD_SESSION, "NotifyPointerAxisDiscrete",
                           GLib.Variant("(ui)", (axis, 1 if steps > 0 else -1)))
                time.sleep(0.02)

    # --------------------------------------------------------------- keyboard
    def keysym(self, sym: int, pressed: bool) -> None:
        rd_path, _ = self._ensure()
        self._call(RD, rd_path, RD_SESSION, "NotifyKeyboardKeysym",
                   GLib.Variant("(ub)", (int(sym), pressed)))

    def type_text(self, text: str, delay: float = 0.012) -> None:
        """Type by character, not by key position.

        The compositor is handed the keysym for the character, so the active
        XKB layout is irrelevant: this is the fix for `de` turning every typed
        `y` into a `z`.
        """
        for ch in str(text):
            sym = char_to_keysym(ch)
            self.keysym(sym, True)
            self.keysym(sym, False)
            time.sleep(delay)

    def combo(self, keys: list[int]) -> None:
        """Press keysyms in order, release in reverse. Modifiers first."""
        for sym in keys:
            self.keysym(sym, True)
            time.sleep(0.02)
        for sym in reversed(keys):
            self.keysym(sym, False)
            time.sleep(0.01)


_SHARED: RemoteInput | None = None


def shared() -> RemoteInput:
    """The process-wide session, because mutter ends one with its connection."""
    global _SHARED
    if _SHARED is None:
        _SHARED = RemoteInput()
    return _SHARED
