#!/usr/bin/env python3
"""Independent GTK/Wayland event witness; does not call Deskwright internals."""
import json
import sys
import time
from pathlib import Path

import cairo
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
events = []
strokes = []
active = None
surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 1000, 600)
ctx = cairo.Context(surface)
ctx.set_source_rgb(1, 1, 1)
ctx.paint()
ctx.set_source_rgb(.12, .3, .7)
ctx.set_line_width(3)
ctx.set_line_cap(cairo.LINE_CAP_ROUND)

win = Gtk.Window(title='Deskwright Stroke Witness')
win.set_default_size(1000, 600)
win.connect('destroy', Gtk.main_quit)
area = Gtk.DrawingArea()
area.set_can_focus(True)
area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                Gdk.EventMask.POINTER_MOTION_MASK)
win.add(area)

def draw(widget, cr):
    cr.set_source_surface(surface, 0, 0)
    cr.paint()

def record(kind, e):
    global active
    event = {'kind':kind, 'x':e.x_root, 'y':e.y_root,
             'local_x':e.x, 'local_y':e.y, 'at':time.monotonic()}
    events.append(event)
    (OUT/'raw-events.json').write_text(json.dumps(events))
    if kind == 'down':
        active = [event]
        ctx.move_to(e.x, e.y)
    elif kind == 'move' and active is not None:
        active.append(event)
        ctx.line_to(e.x, e.y)
        ctx.stroke()
        ctx.move_to(e.x, e.y)
        area.queue_draw()
    elif kind == 'up':
        if active is not None:
            active.append(event)
            strokes.append(active)
        active = None
        surface.write_to_png(str(OUT/'witness.png'))
        temp = OUT/'events.tmp'
        temp.write_text(json.dumps({'strokes':strokes,'events':events,
            'canvas_size':[area.get_allocated_width(),area.get_allocated_height()]},indent=2))
        temp.replace(OUT/'events.json')
    return False

area.connect('draw', draw)
area.connect('button-press-event', lambda w,e: record('down',e))
area.connect('motion-notify-event', lambda w,e: record('move',e))
area.connect('button-release-event', lambda w,e: record('up',e))
key_events=[]
def key_event(widget, event):
    key_events.append({'keyval':event.keyval,'at':time.monotonic()})
    (OUT/'keys.json').write_text(json.dumps(key_events))
    return False
win.connect('key-press-event',key_event)
win.show_all()
area.grab_focus()
Gtk.main()
