"""Disposable GTK surface for scoped queries, commit semantics and canvas fallback.

Launch through launch_app on an owned private desktop. --canvas selects pixels;
the default has one named editing panel and 400 unrelated rows. Writes no files.
"""
import sys

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk  # noqa: E402

canvas = '--canvas' in sys.argv
window = Gtk.Window(title='Hybrid canvas fixture' if canvas else 'Hybrid routing fixture')
window.set_default_size(640, 480)
window.connect('destroy', Gtk.main_quit)
if canvas:
    area = Gtk.DrawingArea()
    def draw(widget, cr):
        cr.set_source_rgb(.12, .22, .34)
        cr.paint()
        cr.set_source_rgb(.95, .65, .2)
        cr.rectangle(70, 80, 260, 150)
        cr.fill()
    area.connect('draw', draw)
    window.add(area)
else:
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    panel.get_accessible().set_name('Editor panel')
    entry = Gtk.Entry()
    entry.get_accessible().set_name('Destination')
    entry.set_text('Original')
    status = Gtk.Label(label='Committed: Original')
    entry.connect('activate', lambda item: status.set_text('Committed: '+item.get_text()))
    button = Gtk.Button(label='Confirm')
    button.connect('clicked', lambda item: item.set_label('Finished'))
    panel.pack_start(entry, False, False, 0)
    panel.pack_start(status, False, False, 0)
    panel.pack_start(button, False, False, 0)
    outer.pack_start(panel, False, False, 0)
    scrolling = Gtk.ScrolledWindow()
    rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for i in range(400):
        rows.pack_start(Gtk.Label(label='Unrelated reference row '+str(i)), False, False, 0)
    scrolling.add(rows)
    outer.pack_start(scrolling, True, True, 0)
    window.add(outer)
window.show_all()
Gtk.main()
