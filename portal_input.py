#!/usr/bin/env python3
"""Pointer and keyboard input over xdg-desktop-portal -- the cross-compositor
route.

WHY THIS EXISTS

remote_input.py talks to `org.gnome.Mutter.RemoteDesktop`, which is GNOME's
private API: no dialog, absolute coordinates, keysyms -- but mutter-only.
`org.freedesktop.portal.RemoteDesktop` is the standardized front door to the
same machinery, implemented on GNOME, KDE and wlroots compositors. This module
is what makes every desktop that is not GNOME reachable, and it is selected
automatically when the mutter API is absent (see input._input()).

The portal's price is a consent dialog on first Start. The spike
(docs/spikes.md, 2026-08-23) proved the whole bargain end to end on this
machine: `persist_mode: 2` + a saved `restore_token` turn the one-time consent
into unattended operation -- the second run reached "session started" with no
dialog at all. Tokens are persisted under
$XDG_STATE_HOME/wayland-computer-use/portal-tokens.json, keyed per session
(the primary desktop and the wcu-headless session have separate portal
backends and separate consents).

The spike's one open wire was absolute motion: `NotifyPointerMotionAbsolute`
returned "Invalid position" because portal absolute coordinates are defined
relative to a SCREENCAST STREAM, and the spike passed a placeholder stream id.
This module closes that wire the way the spec intends: the RemoteDesktop
session doubles as a ScreenCast session (`SelectSources` on the same handle),
`Start` then returns the streams with their position and size in the desktop,
and every (x, y) this module takes is translated into the containing stream's
coordinate space. Nothing ever connects to the PipeWire node; the stream
exists so that coordinates mean something.

PORTAL MECHANICS, STATED ONCE

Every portal method is asynchronous twice over: the call returns a Request
object path, and the actual result arrives later as a Response signal on that
path. The subscription happens BEFORE the call using the documented
predictable path (/org/freedesktop/portal/desktop/request/<sender>/<token>),
because subscribing after is a race the spec warns about. `_request` wraps
the whole dance behind a synchronous call with a timeout.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

# One keysym table, one button table, one gesture layer for every backend.
from remote_input import (  # noqa: E402
    BUTTONS,
    IDLE_TIMEOUT_S,
    KEYSYMS,
    Gestures,
    InputError,
    char_to_keysym,
)

__all__ = ["PortalInput", "InputError", "KEYSYMS", "char_to_keysym", "shared"]

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"

# SelectDevices bitmask: 1 keyboard, 2 pointer.
DEVICES = 3
# SelectSources types: 1 monitor.
MONITOR = 1
# persist_mode 2: "persist until explicitly revoked" -- the whole point.
PERSIST = 2

# How long to wait for a Response. Start gets longer because a human may be
# looking at the consent dialog; everything else is compositor-fast.
RESPONSE_TIMEOUT_S = 15.0
START_TIMEOUT_S = 120.0


def _token_file() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(state, "wayland-computer-use")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "portal-tokens.json")


def _token_key() -> str:
    return "headless" if os.environ.get("WCU_HEADLESS") else "primary"


def _load_token() -> str | None:
    try:
        with open(_token_file()) as f:
            return json.load(f).get(_token_key())
    except (OSError, json.JSONDecodeError):
        return None


def _save_token(token: str | None, forget: bool = False) -> None:
    """Persist this session's restore token, or drop it (`forget`)."""
    if not token and not forget:
        return
    tokens: dict[str, str] = {}
    try:
        with open(_token_file()) as f:
            tokens = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    if forget:
        tokens.pop(_token_key(), None)
    else:
        tokens[_token_key()] = token
    with open(_token_file(), "w") as f:
        json.dump(tokens, f, indent=1)


class PortalInput(Gestures):
    """One portal RemoteDesktop+ScreenCast session, held open while in use."""

    def __init__(self, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
        self._idle_timeout = idle_timeout
        self._lock = threading.RLock()
        self._bus: Any = None
        self._session: str | None = None
        # (node_id, x, y, width, height) per stream, in desktop logical pixels.
        self._streams: list[tuple[int, int, int, int, int]] = []
        self._counter = 0
        self._timer: threading.Timer | None = None
        self.last_position: tuple[float, float] | None = None
        self.last_position_at: float = 0.0

    # ------------------------------------------------------------- transport
    def _connect(self) -> Any:
        if self._bus is None:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error as e:                       # pragma: no cover
                raise InputError(f"no session bus: {e.message}") from None
        return self._bus

    def _call(self, iface: str, method: str, args: GLib.Variant,
              timeout: int = 10000) -> Any:
        bus = self._connect()
        try:
            return bus.call_sync(PORTAL, PORTAL_PATH, iface, method, args,
                                 None, Gio.DBusCallFlags.NONE, timeout, None)
        except GLib.Error as e:
            if method.startswith("Notify") and "not allowed" in e.message:
                # The saved consent covers screen capture but not input: the
                # human left "Allow Remote Interaction" off. Restoring that
                # token forever would wedge every future run in a session that
                # can never click, so the token is discarded here and the next
                # attempt asks again. This is the only self-healing path the
                # backend has, and it exists because the wrong answer is
                # persistent by design.
                _save_token(None, forget=True)
                self.stop()
                raise InputError(
                    "the portal session may capture the screen but not inject "
                    "input -- the consent dialog was approved with 'Allow "
                    "Remote Interaction' switched OFF. The saved token has "
                    "been discarded; the next action will ask for consent "
                    "again. Turn that switch ON when approving."
                ) from None
            raise InputError(f"portal {method} failed: {e.message}") from None

    def _request(self, iface: str, method: str,
                 build_args: Any, timeout: float = RESPONSE_TIMEOUT_S) -> dict:
        """One portal call, waited to its Response, returned as a plain dict.

        `build_args(token)` receives the generated handle_token so the options
        dict can carry it, as the portal requires.
        """
        bus = self._connect()
        self._counter += 1
        token = f"wcu{os.getpid()}n{self._counter}"
        sender = bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        loop = GLib.MainLoop()
        outcome: dict[str, Any] = {}

        def on_response(_bus, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            outcome["code"] = code
            outcome["results"] = results
            loop.quit()

        sub = bus.signal_subscribe(PORTAL, REQUEST, "Response", request_path,
                                   None, Gio.DBusSignalFlags.NO_MATCH_RULE,
                                   on_response)
        try:
            self._call(iface, method, build_args(token),
                       timeout=int(timeout * 1000))
            timer = GLib.timeout_add(int(timeout * 1000), loop.quit)
            loop.run()
            GLib.Source.remove(timer) if "code" in outcome else None
        finally:
            bus.signal_unsubscribe(sub)
        if "code" not in outcome:
            raise InputError(
                f"portal {method} produced no response within {timeout:.0f}s. "
                "If this was Start, a consent dialog may be waiting on the "
                "target session's screen.")
        if outcome["code"] != 0:
            raise InputError(
                f"portal {method} was refused (response code "
                f"{outcome['code']}) -- the consent dialog was cancelled or "
                "the saved restore token was rejected")
        return outcome["results"] or {}

    # --------------------------------------------------------------- session
    def _ensure(self) -> str:
        with self._lock:
            if self._session:
                self._touch()
                return self._session

            results = self._request(
                RD, "CreateSession",
                lambda t: GLib.Variant("(a{sv})", ({
                    "handle_token": GLib.Variant("s", t),
                    "session_handle_token": GLib.Variant("s", f"wcu{os.getpid()}"),
                },)))
            session = results.get("session_handle")
            if not session:
                raise InputError("portal CreateSession returned no session")

            devices_opts = {
                "handle_token": None,       # placed per-call in the lambda
                "types": GLib.Variant("u", DEVICES),
                "persist_mode": GLib.Variant("u", PERSIST),
            }
            token = _load_token()
            if token:
                devices_opts["restore_token"] = GLib.Variant("s", token)

            def build_select_devices(t: str) -> GLib.Variant:
                opts = dict(devices_opts)
                opts["handle_token"] = GLib.Variant("s", t)
                return GLib.Variant("(oa{sv})", (session, opts))

            self._request(RD, "SelectDevices", build_select_devices)

            # The stream is the coordinate space. Without SelectSources the
            # session has no streams and absolute motion has nothing to be
            # relative to -- the exact "Invalid position" the spike hit.
            self._request(
                SC, "SelectSources",
                lambda t: GLib.Variant("(oa{sv})", (session, {
                    "handle_token": GLib.Variant("s", t),
                    "types": GLib.Variant("u", MONITOR),
                    "multiple": GLib.Variant("b", True),
                    # Nothing consumes the frames; a drawn cursor would only
                    # cost the compositor composition work.
                    "cursor_mode": GLib.Variant("u", 1),
                })))

            results = self._request(
                RD, "Start",
                lambda t: GLib.Variant("(osa{sv})", (session, "", {
                    "handle_token": GLib.Variant("s", t),
                })),
                timeout=START_TIMEOUT_S)

            _save_token(results.get("restore_token"))
            streams = []
            for node_id, props in results.get("streams") or []:
                x, y = props.get("position", (0, 0))
                w, h = props.get("size", (0, 0))
                streams.append((int(node_id), int(x), int(y), int(w), int(h)))
            if not streams:
                raise InputError(
                    "portal Start returned no streams; absolute pointer "
                    "coordinates have no coordinate space without one")
            self._session, self._streams = session, streams
            self._touch()
            return session

    def _touch(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle_timeout, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        """End the session. With a saved restore token the next start is
        dialog-free, so idling out costs nothing but the round trips."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._session:
                try:
                    self._connect().call_sync(
                        PORTAL, self._session, SESSION, "Close", None, None,
                        Gio.DBusCallFlags.NONE, 5000, None)
                except GLib.Error:
                    pass                     # already gone; nothing to release
            self._session = None
            self._streams = []

    @property
    def active(self) -> bool:
        return self._session is not None

    # ---------------------------------------------------------------- pointer
    def desktop_bounds(self) -> tuple[int, int, int, int]:
        """The rectangle covering every stream, in desktop logical pixels."""
        self._ensure()
        left = min(s[1] for s in self._streams)
        top = min(s[2] for s in self._streams)
        right = max(s[1] + s[3] for s in self._streams)
        bottom = max(s[2] + s[4] for s in self._streams)
        if right <= left or bottom <= top:
            raise InputError("portal streams report no usable size")
        return left, top, right - left, bottom - top

    def _stream_for(self, x: float, y: float) -> tuple[int, float, float]:
        for node, sx, sy, w, h in self._streams:
            if sx <= x <= sx + w and sy <= y <= sy + h:
                return node, x - sx, y - sy
        bounds = self.desktop_bounds()
        raise InputError(
            f"({x:.0f}, {y:.0f}) is outside every stream; the desktop is "
            f"{bounds[2]}x{bounds[3]} at ({bounds[0]}, {bounds[1]}). "
            "Nothing was moved.")

    def move_to(self, x: float, y: float) -> tuple[float, float]:
        session = self._ensure()
        node, sx, sy = self._stream_for(x, y)
        self._call(RD, "NotifyPointerMotionAbsolute",
                   GLib.Variant("(oa{sv}udd)", (session, {}, node, sx, sy)))
        self.last_position = (x, y)
        self.last_position_at = time.time()
        return x, y

    def button(self, name: str, pressed: bool) -> None:
        code = BUTTONS.get(str(name).lower())
        if code is None:
            raise InputError(
                f"unknown button {name!r}. Known: {', '.join(sorted(BUTTONS))}")
        session = self._ensure()
        self._call(RD, "NotifyPointerButton",
                   GLib.Variant("(oa{sv}iu)", (session, {}, code,
                                               1 if pressed else 0)))

    def scroll(self, x: float, y: float, dy: int = 0, dx: int = 0) -> None:
        """Wheel clicks at a point. Positive dy scrolls down, dx scrolls right."""
        self.move_to(x, y)
        time.sleep(0.04)
        session = self._ensure()
        for axis, steps in ((0, int(dy)), (1, int(dx))):
            if not steps:
                continue
            for _ in range(abs(steps)):
                self._call(RD, "NotifyPointerAxisDiscrete",
                           GLib.Variant("(oa{sv}ui)",
                                        (session, {}, axis,
                                         1 if steps > 0 else -1)))
                time.sleep(0.02)

    # --------------------------------------------------------------- keyboard
    def keysym(self, sym: int, pressed: bool) -> None:
        session = self._ensure()
        self._call(RD, "NotifyKeyboardKeysym",
                   GLib.Variant("(oa{sv}iu)", (session, {}, int(sym),
                                               1 if pressed else 0)))

    def type_text(self, text: str, delay: float = 0.012) -> None:
        """Type by character, not by key position -- see remote_input.py."""
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


_SHARED: PortalInput | None = None


def shared() -> PortalInput:
    """The process-wide session, mirroring remote_input.shared()."""
    global _SHARED
    if _SHARED is None:
        _SHARED = PortalInput()
    return _SHARED
