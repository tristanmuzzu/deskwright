"""Low-latency identity and halt checks during continuous input."""
from __future__ import annotations

import json

from . import shell
from .errors import ToolError


class PathGuard:
    """Reuse a D-Bus connection instead of spawning gdbus while drawing.

    The first live witness measured 70-80 ms event gaps when four subprocess
    queries ran every 100 ms. WindowAt already returns identity and geometry,
    so two direct calls prove the same conditions without those stalls.
    """
    def __init__(self, window: dict):
        from gi.repository import Gio, GLib
        shell._pick_bus()
        self.window = window
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name, self.path = shell.BUS_NAME, shell.OBJ_PATH

    def _call(self, method, parameters=None):
        try:
            return self.bus.call_sync(self.name, self.path, self.name, method,
                parameters, None, self.Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        except Exception as e:
            raise ToolError(f'continuous input lost its compositor check: {e}',
                            code='extension_unavailable') from e

    def __call__(self, x: float, y: float) -> None:
        if self._call('HaltActive'):
            raise ToolError('human halt switch engaged during path', code='halted')
        hit = json.loads(self._call('WindowAt', self.GLib.Variant('(ii)', (int(x), int(y)))))
        window = hit.get('window')
        if not window or window.get('id') != self.window['id']:
            raise ToolError('path target is occluded; inspect the desktop before continuing',
                            code='occluded')
        if any(window.get(k) != self.window[k] for k in ('x','y','width','height')):
            raise ToolError('target moved during path; observe before retrying', code='widget_moved')
