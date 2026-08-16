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
| `ydotool mousemove --absolute` | Silently does nothing here. Relative motion works but is put through mutter's pointer acceleration, so units are not pixels: measured 2026-08-16, 5 units moved 2 px and 200 units moved off the edge of the screen |
| `xdotool click` under XWayland | Not routed to the compositor, and asking pops an `xdg-desktop-portal-gnome` "Remote Desktop / Allow Remote Interaction" dialog that grabs input until it is dismissed |
| `ReloadExtension` over D-Bus | `NotSupported: ReloadExtension is deprecated and does not work` on 50.1. An edited extension is not running until the next login, and disable/enable does not help — gnome-shell has already imported the module |

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

Keystrokes from `desktop.py` go through `ydotool` and `/dev/uinput`, below the
compositor. This is **focus-blind** — it types wherever focus happens to be, so
call `activate` first — and it is also **layout-blind**: ydotool sends
US-QWERTY keycodes, the compositor maps them through the active layout, and on
this machine's `de` layout a typed `y` arrives as `z`. The MCP server does not
have this problem; it sends keysyms through the compositor instead.

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
./tests/test_pointer.py                # prove the pointer lands where it is told
./tests/test_screencast.py             # prove a recording is a real recording
```

The ordering below is the ordering you should prefer:

| Tool | Notes |
|---|---|
| `ui_apps`, `ui_tree`, `ui_find` | Locate things. `ui_find` searches to depth 30 by default — see below. |
| `ui_read_text` | Read a text widget's content. This is how you *verify* something landed. |
| `ui_set_text` | **Preferred text entry.** AT-SPI `EditableText`: no focus, no keyboard, and it reads the widget back to prove the write. |
| `ui_press` | **Preferred action.** Invokes the widget's own action. Requires `expect_name`/`expect_role`. |
| `screen_map` | Where everything is, in pixels: windows top of the stack first with their centres, and every pressable widget of the focused app with the point to click it at. |
| `window_at` | What a click at a point would hit, *before* clicking it. |
| `pointer_click`, `pointer_move`, `pointer_drag`, `pointer_scroll` | Real pointer input at absolute screen coordinates. Pass `expect_window` and a click that would land elsewhere is refused. |
| `pointer_position` | Where the pointer is, or an honest statement that only the last set position is known. |
| `wait_for` | Wait for `window_exists` / `window_gone` / `window_focused` / `focus_changes` instead of sleeping a guessed number of seconds. |
| `list_windows`, `activate_window`, `screenshot` | Extension-backed. Unavailable while the screen is locked. |
| `screencast`, `frames` | For anything that moves. Stills cannot show motion. |
| `type_text`, `press_keys` | Compositor keysyms by default, focus proven first, `ydotool` only as a fallback. |
| `desktop_health` | Which mechanisms are usable right now, what each will actually do, and which extension methods the *running* shell has. |

### Pointing, and how to know where to point

The pointer goes through `org.gnome.Mutter.RemoteDesktop` (see
`remote_input.py`), which takes absolute coordinates in the same space
`list_windows` reports geometry in. No acceleration curve, no consent dialog,
no closed loop. Proven by `tests/test_pointer.py`, which puts a witness window
on screen and asserts against what it actually received.

Three ways to turn "click that button" into a number, best first:

1. `ui_press` — do not click at all. Press the widget.
2. `screen_map` — the widget list already carries `click_at` coordinates taken
   from the accessibility tree, so no measuring off an image.
3. `screenshot` with `annotate` — draws the grid, the window boxes and the
   widget boxes onto the picture, labelled in **screen** coordinates, so the
   number to click can be read off the image rather than estimated from
   proportions. Combine with `scale` to keep it cheap.

Then click with `expect_window`. The click is refused if the compositor would
deliver it to a different window, which is the difference between a missed
click and a click in someone else's window.

While the pointer session is open, GNOME shows its orange screen-sharing
indicator in the top bar. That is the price of the API, and it doubles as a
visible sign that something else is driving the machine. The session closes
itself after 25 idle seconds.

### Half of the extension work needs a logout

`Pointer`, `WindowAt`, `ScreenshotArea` and `ScreenshotWindow` are implemented
in the extension source, but gnome-shell imports an extension once per session
and **cannot reload it on Wayland**. Until the next login the server falls back:
`pointer_position` reports the last position it set and says so, `window_at`
uses window rectangles and warns that an input-shaped overlay will fool it, and
region captures are cropped client-side. `desktop_health` lists exactly which
methods the running shell has.

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
  Wayland without ending the session. Its source lives in
  `~/projects/gnome-migration-helpers/` and is installed to
  `~/.local/share/gnome-shell/extensions/`.
- `python3-gi` and Pillow (`python3-pil`). PyGObject is what talks to
  `org.gnome.Mutter.RemoteDesktop`; Pillow does the cropping, scaling and
  annotation.
- `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
- `ydotoold` as a **system** service with its socket at `/run/ydotoold.socket`
  — only needed now for the `via: "ydotool"` fallback.
