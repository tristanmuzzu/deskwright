"""Disposable GTK form with a real save/reopen oracle for interface trials."""
import json
import sys
from pathlib import Path

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk  # noqa: E402

GLib.set_prgname('deskwright-contract-form')
GLib.set_application_name('Deskwright Contract Form')
out = Path(sys.argv[1])
win = Gtk.Window(title='Deskwright Contract Form')
win.set_default_size(600, 420)
win.connect('destroy', Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
win.add(box)
fields = {}
for name in ['Name', 'City', 'Reference', 'Notes', 'Status']:
    row = Gtk.Box(spacing=10)
    label = Gtk.Label(label=name, width_chars=12, xalign=0)
    entry = Gtk.Entry()
    entry.get_accessible().set_name(name)
    row.pack_start(label, False, False, 0)
    row.pack_start(entry, True, True, 0)
    box.pack_start(row, False, False, 0)
    fields[name] = entry
status = Gtk.Label(label='Ready')


def save(_):
    out.write_text(json.dumps({k: e.get_text() for k, e in fields.items()}, ensure_ascii=False))
    status.set_text('Saved')


def reopen(_):
    for k, value in json.loads(out.read_text()).items():
        fields[k].set_text(value)
    status.set_text('Reopened')


for name, handler in [('Save', save), ('Reopen', reopen)]:
    button = Gtk.Button(label=name)
    button.connect('clicked', handler)
    box.pack_start(button, False, False, 0)
box.pack_start(status, False, False, 0)
win.show_all()
Gtk.main()
