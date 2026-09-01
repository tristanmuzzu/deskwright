# Computer Use Helpers (GNOME Shell extension)

The compositor-side half of deskwright: a small D-Bus service
(`com.zeticle.deskwright` at `/com/zeticle/deskwright`) exported from inside gnome-shell,
providing screenshots, window list/geometry/control, pointer position,
clipboard read/write, an on-screen "agent is driving" border, and a
human-only halt keybinding (`<Super><Ctrl>Escape`).

## Why an extension

GNOME on Wayland refuses these to ordinary clients; the documented routes were
tried first and denied: `org.gnome.Shell.Screenshot` answers
`AccessDenied: Screenshot is not allowed`, `GrabAccelerator` answers
`AccessDenied: GrabAccelerator is not allowed`, and there is no protocol at all
for a client to read the pointer, learn window geometry, or raise a window.
Inside the compositor every one of these is an ordinary call, so that is where
this code lives. (Full denial table in the main [README](../../README.md).)

## Install

```sh
cp -r extension/deskwright@zeticle.com ~/.local/share/gnome-shell/extensions/
gnome-extensions enable deskwright@zeticle.com
```

Then **log out and back in**. This is not optional. On Wayland gnome-shell
cannot be restarted in place, `ReloadExtension` is deprecated and does not
work, and `gnome-extensions enable` alone will not import the code into the
running shell. The same applies after every edit to the extension: a changed
file is not running until the next login (check with the `Ping` method, which
returns the loaded build stamp).

Before logging out, verify the shell will accept the files:

```sh
extension/deskwright@zeticle.com/check-syntax.sh
```

## Notes

- **Runs while locked (deliberate):** `session-modes` includes
  `unlock-dialog`, so screenshots, window queries and the halt keybinding keep
  working when the screen locks mid-run instead of silently dying.
- **Halt:** `<Super><Ctrl>Escape` sets a session-scoped flag (poll
  `HaltActive`) and paints a HALTED border; pressing it again clears it,
  except within 2 seconds of engaging (debounce against an injected
  double-press; see the limitation note in `dbus.js`).
- **Coexistence:** fully independent of `migration-helpers@tristan.local` if
  that is installed, different UUID, bus name, settings schema, indicator
  color, and no shared state. Neither needs, touches, or conflicts with the
  other.
- `schemas/gschemas.compiled` ships in the repo; if you edit the schema XML,
  recompile with `glib-compile-schemas schemas/`.
