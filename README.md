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

## `mcp_server.py` — the way this is actually meant to be used

The CLIs above are for poking at things by hand. In a session, use the MCP
server: it is registered at user scope, so "do this on my laptop" works without
remembering a script path.

```bash
./mcp_server.py --self-test            # prove every capability, print a report
./tests/test_e2e_real_task.py          # drive a real task through the protocol
```

Twelve tools. The ordering below is the ordering you should prefer:

| Tool | Notes |
|---|---|
| `ui_apps`, `ui_tree`, `ui_find` | Locate things. `ui_find` searches to depth 30 by default — see below. |
| `ui_read_text` | Read a text widget's content. This is how you *verify* something landed. |
| `ui_set_text` | **Preferred text entry.** AT-SPI `EditableText`: no focus, no keyboard, and it reads the widget back to prove the write. |
| `ui_press` | **Preferred action.** Invokes the widget's own action. Requires `expect_name`/`expect_role`. |
| `list_windows`, `activate_window`, `screenshot` | Extension-backed. Unavailable while the screen is locked. |
| `type_text`, `press_keys` | Last resort. `ydotool` injection, focus proven first. |
| `desktop_health` | Which mechanisms are usable right now, and why not if not. |

Three things the server does that the CLIs did not:

**Focus is proven, not assumed.** Every injecting tool takes a `target` window,
activates it, then polls `ListWindows` until that window really reports
`focused: true`. If focus never lands it returns an error and types nothing.
Focus-blind injection into the wrong window is the easiest way to do real damage
here.

**Widget identity is re-checked before acting.** An AT-SPI index path is valid
only while the tree is unchanged, so `ui_press` refuses unless you state the name
or role you expect and the resolved widget still matches.

**`ui_set_text` sidesteps focus entirely.** AT-SPI hands characters straight to
the widget. It works on an unfocused window, works while the screen is locked,
and verifies itself. Proven end to end: 16 characters written into
gnome-text-editor and read back out of the tree, with the screen locked.

### GTK4 nests deeper than you think

gnome-text-editor's document text view sits at **depth 23**, behind a stack of
anonymous panels and groupings; its Main Menu button is at depth 18. A depth-8
search returns nothing at all, which reads as "this app has no widgets" rather
than "you did not look far enough". `ui_find` therefore defaults to depth 30 and
caps on node count instead.

### The screen lock takes half of this away

gnome-shell unloads every extension whose `metadata.json` does not list
`unlock-dialog` in `session-modes`, and a screen lock is exactly that change of
session mode. `migration-helpers` lists only `user`, so **while the screen is
locked there are no screenshots, no window list, and no focus control.** The
extension reports `State: INACTIVE` and D-Bus calls fail — which looks identical
to the extension being broken and has a completely different remedy. Both
`desktop_health` and `~/system/healthcheck.sh` now tell the two apart by reading
`session-modes`.

AT-SPI is unaffected: `ui_find`, `ui_press`, `ui_set_text` and `ui_read_text` all
keep working while locked. That is a second reason to prefer them.

Adding `"unlock-dialog"` to `session-modes` (plus one logout) would keep the whole
surface alive while locked. It is deliberately **not** done here: it also makes
screenshots of the locked desktop possible, which is Tristan's call to make, not
an agent's.

## Requirements

- The `migration-helpers@tristan.local` extension, loaded and enabled. It is
  only picked up at session start; there is no way to reload gnome-shell on
  Wayland without ending the session.
- `ydotoold` running as a **system** service with its socket at
  `/run/ydotoold.socket`.
- `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
