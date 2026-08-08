# wayland-computer-use

Desktop automation for **GNOME Shell 50 on Wayland**, where most of the usual
tools do not work and fail in ways that look like your own mistake.

The original plan for this machine assumed Xorg and `xdotool`. That is dead:
Ubuntu 26.04 ships no Xorg session and one cannot be installed, and XWayland
runs X11 *applications* without giving anyone global input or window control.
So this is built on what Wayland actually permits.

## What does not work, and why

Each of these was tried first and refused. They are listed because every one of
them is documented somewhere as the way to do it.

| Approach | What happens |
|---|---|
| `xdotool` / `wmctrl` for global input or window control | Only ever sees XWayland clients. Useless for native Wayland windows. |
| `org.gnome.Shell.Screenshot` over D-Bus | `AccessDenied: Screenshot is not allowed` |
| `org.gnome.Shell.GrabAccelerator` over D-Bus | `AccessDenied: GrabAccelerator is not allowed` — this is why GNOME "custom shortcuts" silently never fire on this box |
| `grim` | wlroots-only; GNOME does not implement the protocol |
| Asking where the pointer is | Not permitted to any client. `cursor_position()` returns the centre of the screen forever, even under XWayland |
| A client raising itself | No protocol for it in GNOME (no `wlr-layer-shell`) |

## What does work

**`atspi_ui.py` — the accessibility tree. Start here.**
Every application exposes its real widgets with roles, names and invokable
actions. Pressing the actual button beats clicking a pixel: it cannot miss, it
cannot be defeated by a window moving, and it needs no pointer.

```bash
./atspi_ui.py apps
./atspi_ui.py tree "Google Chrome" --depth 6
./atspi_ui.py find "Reload" --role "push button"
./atspi_ui.py actions "gnome-tweaks/0"
./atspi_ui.py do "gnome-tweaks/0" 0
```

One hard requirement: **`toolkit-accessibility` must be true before an
application starts.** Applications launched before it was enabled expose a
stunted tree — panels and groupings with no buttons in them — which looks like
the tree is simply empty.

```bash
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

Screen coordinates in the tree read `@0,0` under Wayland, because a client does
not know where it is. Use the tree for *what* to press, never for *where*.

**`desktop.py` — screenshots, window list, focus, keystrokes.**

```bash
./desktop.py windows
./desktop.py activate <id>
./desktop.py screenshot shot.png
./desktop.py type "hello"
./desktop.py key ctrl+s
```

Screenshots, the window list and focus control come from the **Migration
Helpers GNOME Shell extension** over D-Bus (`org.tristan.MigrationHelpers`).
An extension runs inside gnome-shell, so the calls the compositor refuses to a
client are ordinary calls to it. The extension lives in `~/system`.

Keystrokes go through `ydotool` and `/dev/uinput`, below the compositor. This
is **focus-blind** — it types wherever focus happens to be, so call `activate`
first.

### One combination is refused outright

`Ctrl+Alt+F1` … `F12` is `switch-to-session` in mutter. Injecting one throws the
desktop onto a different virtual terminal showing a login screen, which is
indistinguishable from a frozen machine — it cost a session and a hard
power-off on 2026-08-08. `desktop.py key` refuses it rather than trusting the
caller to remember.

## Requirements

- The `migration-helpers@tristan.local` extension, loaded and enabled. It is
  only picked up at session start; there is no way to reload gnome-shell on
  Wayland without ending the session.
- `ydotoold` running as a **system** service with its socket at
  `/run/ydotoold.socket`.
- `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
