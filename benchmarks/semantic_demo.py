"""Visible, disposable desktop demonstration with an independent saved-file oracle.

Only the supplied NEW output directory is written. Run through launch_app on
the requested desktop. All controls are real GTK widgets, not a web simulation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

GLib.set_prgname("deskwright-semantic-demo")
GLib.set_application_name("Deskwright Semantic Demo")
output = Path(sys.argv[1]).resolve()
output.mkdir(parents=True, exist_ok=False)
window = Gtk.Window(title="Deskwright | Semantic desktop demonstration")
window.set_default_size(1120, 780)
window.connect("destroy", Gtk.main_quit)
outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=24)
window.add(outer)
heading = Gtk.Label(xalign=0)
heading.set_markup('<span size="xx-large" weight="bold">Plan, review, save, recover</span>')
outer.pack_start(heading, False, False, 0)
outer.pack_start(Gtk.Label(label="Disposable native controls. All output stays in " + str(output),
                          xalign=0, selectable=True), False, False, 0)
grid = Gtk.Grid(column_spacing=18, row_spacing=10)
outer.pack_start(grid, False, False, 0)
fields = {}
names = ["Project", "Owner", "Location", "Reference", "Task 1", "Assignee 1", "Due 1",
         "Task 2", "Assignee 2", "Due 2", "Task 3", "Assignee 3", "Due 3", "Notes"]
for i, name in enumerate(names):
    column, row = (i % 2) * 2, i // 2
    label = Gtk.Label(label=name, xalign=0)
    entry = Gtk.Entry(hexpand=True)
    entry.get_accessible().set_name(name)
    grid.attach(label, column, row, 1, 1)
    grid.attach(entry, column + 1, row, 1, 1)
    fields[name] = entry
checks = {}
check_row = Gtk.Box(spacing=24)
for name in ("Include checklist", "Enable review", "Mark ready"):
    widget = Gtk.CheckButton(label=name)
    checks[name] = widget
    check_row.pack_start(widget, False, False, 0)
outer.pack_start(check_row, False, False, 0)
status = Gtk.Label(label="Ready for the demonstration", xalign=0)
status.get_accessible().set_name("Run status")
outer.pack_start(status, False, False, 0)
history = Gtk.TextView(editable=False, cursor_visible=False)
history.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
history.get_accessible().set_name("Verification log")
scroll = Gtk.ScrolledWindow(vexpand=True)
scroll.add(history)
outer.pack_start(scroll, True, True, 0)


def record(message):
    buffer = history.get_buffer()
    buffer.insert(buffer.get_end_iter(), message + "\n")
    status.set_text(message)


def values():
    return {"fields": {name: entry.get_text() for name, entry in fields.items()},
            "options": {name: widget.get_active() for name, widget in checks.items()}}


def review(_):
    snapshot = values()
    if any(not value for value in snapshot["fields"].values()):
        record("Validation refused: every field must be filled.")
        return
    dialog = Gtk.Dialog(title="Review demonstration package", transient_for=window,
                        modal=True, destroy_with_parent=True)
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Save package", Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    label = Gtk.Label(label="Review: " + snapshot["fields"]["Project"]
                      + "\n14 fields and 3 options will be saved locally."
                      + "\nNo messages, uploads or account changes.", margin=24)
    content.add(label)

    def respond(dlg, response):
        if response == Gtk.ResponseType.OK:
            (output / "package.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            report = "Semantic desktop demonstration\n\n"
            report += "\n".join(f"{name}: {value}" for name, value in snapshot["fields"].items())
            report += "\n\n" + "\n".join(f"{name}: {value}" for name, value in snapshot["options"].items())
            (output / "report.txt").write_text(report + "\n")
            record("Saved package.json and report.txt after review.")
        dlg.destroy()

    dialog.connect("response", respond)
    dialog.show_all()


def clear(_):
    for entry in fields.values():
        entry.set_text("")
    for widget in checks.values():
        widget.set_active(False)
    record("Cleared the live form. Saved files remain intact.")


def reopen(_):
    saved = json.loads((output / "package.json").read_text())
    for name, value in saved["fields"].items():
        fields[name].set_text(value)
    for name, value in saved["options"].items():
        checks[name].set_active(value)
    record("Reopened all 14 fields and 3 options from disk.")


def verify(_):
    saved = json.loads((output / "package.json").read_text())
    if saved != values():
        record("FAILED: the form differs from the saved package.")
        return
    (output / "verified.json").write_text(json.dumps({"exact": True, "fields": 14, "options": 3}))
    record("PASS: all 14 fields and 3 options exactly match the saved package.")


buttons = Gtk.Box(spacing=12)
for title, handler in (("Review package", review), ("Clear form", clear),
                       ("Reopen package", reopen), ("Verify recovery", verify)):
    button = Gtk.Button(label=title)
    button.connect("clicked", handler)
    buttons.pack_start(button, True, True, 0)
outer.pack_start(buttons, False, False, 0)
window.show_all()
Gtk.main()
