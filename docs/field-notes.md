# Field notes

Everything below was measured on this machine, not assumed. The dates are kept
because each finding was true of a specific GNOME on a specific day, and Wayland
moves. Re-measure before you trust one of these against a newer shell.

If you're just trying to use the thing, you want the [README](../README.md).
This is the file for when something behaves oddly and you want to know why, or
when you're about to change the code and would rather not rediscover a wall the
hard way.

## What does not work, and why

Each of these was tried first and refused. They are listed because every one of
them is documented somewhere as the way to do it.

| Approach | What happens |
|---|---|
| `xdotool` / `wmctrl` for global input or window control | Only ever sees XWayland clients. Useless for native Wayland windows. |
| `org.gnome.Shell.Screenshot` over D-Bus | `AccessDenied: Screenshot is not allowed` |
| `org.gnome.Shell.GrabAccelerator` over D-Bus | `AccessDenied: GrabAccelerator is not allowed`, this is why GNOME "custom shortcuts" configured by a script silently never fire |
| `grim` | wlroots-only; GNOME does not implement the protocol |
| Asking where the pointer is | Not permitted to any client. `cursor_position()` returns the centre of the screen forever, even under XWayland |
| A client raising itself | No protocol for it in GNOME (no `wlr-layer-shell`) |
| `ydotool mousemove --absolute` | Silently does nothing here. Relative motion works but is put through mutter's pointer acceleration, so units are not pixels: measured 2026-08-16, 5 units moved 2 px and 200 units moved off the edge of the screen |
| `xdotool click` under XWayland | Not routed to the compositor, and asking pops an `xdg-desktop-portal-gnome` "Remote Desktop / Allow Remote Interaction" dialog that grabs input until it is dismissed |
| `ReloadExtension` over D-Bus | `NotSupported: ReloadExtension is deprecated and does not work` on GNOME 50.1. An edited extension is not running until the next login, and disable/enable does not help, gnome-shell has already imported the module |

## What does work

**The accessibility tree. Start here.**
Every application exposes its real widgets with roles, names and invokable
actions. Pressing the actual button beats clicking a pixel: it cannot miss, it
cannot be defeated by a window moving, and it needs no pointer. The `ui_*`
tools are built on it; `wcu/atspi_ui.py` is the standalone CLI for poking at it by
hand:

```bash
wcu-atspi apps
wcu-atspi tree "Google Chrome" --depth 6
wcu-atspi find "Reload" --role "push button"
wcu-atspi actions "gnome-tweaks/0"
wcu-atspi do "gnome-tweaks/0" 0
```

One hard requirement: **`toolkit-accessibility` must be true before an
application starts.** Applications launched before it was enabled expose a
stunted tree, panels and groupings with no buttons in them, which looks like
the tree is simply empty.

```bash
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

Screen coordinates in the tree read `@0,0` under Wayland, because a client does
not know where it is. Use the tree for *what* to press, never for *where*.

**The compositor itself, via the bundled extension.**
Screenshots, the window list, focus control, window management, pointer
position and the halt switch come from the **bundled GNOME Shell extension**
(`wcu/extension/wcu@wayland-computer-use`, D-Bus name `org.wcu.Helpers`). An
extension runs inside gnome-shell, so the calls the compositor refuses to a
client are ordinary calls to it. `wcu/desktop.py` is the standalone CLI over the
same mechanisms:

```bash
wcu-desktop windows
wcu-desktop activate <id>
wcu-desktop screenshot shot.png
wcu-desktop type "hello"
wcu-desktop key ctrl+s
```

Keystrokes from `wcu/desktop.py` go through `ydotool` and `/dev/uinput`, below the
compositor. This is **focus-blind**: it types wherever focus happens to be, so
call `activate` first, and it is also **layout-blind**: ydotool sends
US-QWERTY keycodes and the compositor maps them through the active layout, so
on a `de` layout a typed `y` arrives as `z`. The MCP server does not have this
problem; it sends keysyms through the compositor instead.

### One combination is refused outright

`Ctrl+Alt+F1` … `F12` is `switch-to-session` in mutter. Injecting one throws the
desktop onto a different virtual terminal showing a login screen, which is
indistinguishable from a frozen machine. It cost a session and a hard
power-off on 2026-08-08. `wcu-desktop key` and the server both refuse it rather
than trusting the caller to remember.

## The MCP server, the way this is actually meant to be used

The CLIs above are for poking at things by hand. In a session, use the MCP
server: registered at user scope, "do this on my laptop" works without
remembering a script path.

An installed copy proves itself with one command, on a desktop you cannot see:

```bash
WCU_SESSION=headless wayland-computer-use --self-test
```

The rest are **from a checkout**, the live suites are not in the wheel,
because they need a real session and a loaded extension:

```bash
./mcp_server.py --self-test            # prove every capability, print a report
./tests/test_look.py                   # prove it SHOWS you things, and hit != miss
./tests/test_e2e_real_task.py          # drive a real task through the protocol
./tests/test_pointer.py                # prove the pointer lands where it is told
./tests/test_screencast.py             # prove a recording is a real recording
./tests/mcpdrv.py tools                # speak MCP to a fresh server, from a shell
```

`tests/mcpdrv.py` matters more than it looks: the server an MCP client is
holding open is whatever was on disk when the session started, so without it no
change here is observable until a restart.

### The round trip is the cost, not the work

Measured from real agent session transcripts, 2026-08-22, on the development
machine, one client (Claude Code). These are the numbers that shaped the tool
surface, not a general claim about agents:

| | |
|---|---|
| a screenshot capture | **0.23 s** |
| the same screenshot, then a `Read` of the PNG, then the next action | **14.0 s** (median, n=28) |
| a tool that returns its image inline, then the next action | **7.9 s** (median, n=37) |
| screenshots followed by a `Read` in one 102-minute session | **61 of 62** |
| assistant messages in two long sessions containing more than one tool call | **0 of 1052** |

Everything in this server that looks like a convenience is really that table.
Images come back **inside the reply**; acting tools **show you the result**
instead of making you ask; `do_steps` runs a known sequence in one call; and
`find_text` and `region_changed` answer questions that were previously answered
by taking a picture and looking at it.

Two things that are NOT true and were assumed to be:

* **`scale` is not a cheap way to look at the screen.** Measured on a
  1920x1080 panel, a full capture reduced to 960x540 loses small UI text
  entirely, OCR reads 0 words against 106 at 1568px, and a human reading it
  struggles. Crop instead: a window is ~1300 tokens and a 1200x100 strip is 160,
  against 1843 for the whole desktop, and all of them stay legible.
* **"percent of pixels changed" cannot tell a hit from a miss.** A real button
  press moves 0.05% of a window. What separates them is contrast: a miss moves
  0 cells by more than 60/255, the smallest real press moves 22.

### Pointing, and how to know where to point

The pointer goes through `org.gnome.Mutter.RemoteDesktop` (see
`wcu/remote_input.py`), which takes absolute coordinates in the same space
`list_windows` reports geometry in. No acceleration curve, no consent dialog,
no closed loop. Proven by `tests/test_pointer.py`, which puts a witness window
on screen and asserts against what it actually received.

Four ways to turn "click that button" into a number, best first:

1. `ui_press`, do not click at all. Press the widget.
2. `screen_map`, the widget list already carries `click_at` coordinates taken
   from the accessibility tree, so no measuring off an image.
3. `find_text`, OCR, returning `click_at` in screen coordinates. Slower than
   the tree and blind to icon-only buttons, but it reads Chrome, Electron and
   Qt, where the tree is empty.
4. `screenshot` with `annotate`, draws the grid, the window boxes and the
   widget boxes onto the picture, labelled in **screen** coordinates, so the
   number to click can be read off the image rather than estimated from
   proportions. Crop it with `window` or `region`; do not shrink it with
   `scale`.

Whatever you clicked with, the click tells you whether it landed: the screen is
compared before and after, and a click into dead space says so instead of
looking exactly like one that worked.

Then click with `expect_window`. The click is refused if the compositor would
deliver it to a different window, which is the difference between a missed
click and a click in someone else's window.

While the pointer session is open, GNOME shows its orange screen-sharing
indicator in the top bar. That is the price of the API, and it doubles as a
visible sign that something else is driving the machine. The session closes
itself after 25 idle seconds.

### Chrome and Electron are opaque, and there is a flag for it

Measured 2026-08-22, same binary, same moment, one tab each:

| | AT-SPI nodes | actionable |
|---|---|---|
| Chrome, as it launches today | 7 | **3** |
| Chrome with `--force-renderer-accessibility` | 238 | **238** |

With the flag the whole page is exposed with real screen bounds, headings,
links, the omnibox as a readable `entry`. **Proven end to end 2026-08-22:**
`ui_find` located a page's own `<button>` at (83, 241, 183, 52) and `ui_press`
activated it, with the page's JavaScript reacting. No coordinates, no pixels,
cannot miss. The same flag applies to Electron applications.

Cost, measured on the same machine:

| page | renderer RSS | total RSS | idle CPU |
|---|---|---|---|
| one trivial tab | +6 MB (+2%) | +26 MB | none measurable |
| a 24 000-node DOM | −19 MB | **+111 MB (+7.6%)** | 0.65% vs 0.55% of a core |

**It was deliberately NOT enabled** on the machine where these numbers were
taken, and the reasoning is worth keeping because the measurements alone look
favourable, it is a worked example of the trade, not a universal verdict:

* A browser-native MCP tool (there, `claude-in-chrome`) was already driving
  the user's real Chrome with `read_page` and CDP, which is strictly better
  than an accessibility tree for web work. The flag mostly duplicates a tool
  that is already present.
* The memory cost lands wherever RAM is the accepted constraint. 111 MB on a
  heavy page is not free on an 8 GB machine.
* It cannot be turned on per-task. Chrome reuses its running process, so
  `google-chrome --force-renderer-accessibility` opens a window in the existing
  instance and the flag does nothing. It is all-or-nothing per Chrome process,
  and a separate profile has none of the logins that make driving a browser
  worth doing.

The one case where it wins outright: a headless or scheduled run, where
interactively-authenticated MCP servers may not be available at all, and
AT-SPI still is. To use it there, launch the browser with the flag rather than
attaching to a running one:

```bash
google-chrome --force-renderer-accessibility --user-data-dir=/tmp/a11y-profile
```

It is written down because "Chrome exposes nothing" was treated as a property of
Chrome for months, and it is a property of one flag.

### Three things the server does that the CLIs did not

**Focus is proven, not assumed.** Every injecting tool takes a `target` window,
activates it, then polls `ListWindows` until that window really reports
`focused: true`. If focus never lands it returns an error and types nothing.
Focus-blind injection into the wrong window is the easiest way to do real
damage.

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

### The screen lock, and what it takes away

gnome-shell unloads every extension whose `metadata.json` does not list
`unlock-dialog` in `session-modes`, and a screen lock is exactly that change of
session mode. An extension without it reports `State: INACTIVE` and every D-Bus
call fails, which looks identical to the extension being broken and has a
completely different remedy. `desktop_health` tells the two apart by reading
`session-modes` at the time it is asked.

The bundled extension lists `["user", "unlock-dialog"]`, so screenshots, the
window list, focus control and the halt switch survive a lock. That is a
deliberate choice with a cost attached: it also means the desktop can be
screenshotted while locked. Dropping `unlock-dialog` reverses both, see
[SECURITY.md](../SECURITY.md).

AT-SPI is unaffected either way: `ui_find`, `ui_press`, `ui_set_text` and
`ui_read_text` keep working while locked. That is a second reason to prefer
them.

The pointer is a third case. `org.gnome.Mutter.RemoteDesktop` is mutter's, not
the extension's, so it does not care about session modes, but there is nothing
worth clicking on a lock screen, and clicking blind is exactly what the guards
exist to prevent.

### Extension changes need a logout

gnome-shell imports an extension once per session and **cannot reload it on
Wayland**, `ReloadExtension` is deprecated and disable/enable does not
re-import the module. Until the next login after an install or edit, the
server degrades honestly: `pointer_position` reports the last position it set
and says so, `window_at` falls back to window rectangles and warns that an
input-shaped overlay will fool it, and region captures are cropped
client-side. `desktop_health` lists exactly which methods the running shell
has, and the extension's `Ping` method returns the loaded build stamp.

## Two input backends: mutter, and the portal (KDE/wlroots route)

**This is the input half only, and that is why the support matrix still says
GNOME.** The portal backend really does drive the pointer and keyboard on KDE
and wlroots today. What has no cross-compositor route yet is everything the
gnome-shell extension provides, the window list and window verbs,
extension-side screenshots, pointer position, and the halt switch, so on
Plasma or Sway `desktop_health` reports *not usable* and means it. Window
enumeration is the remaining piece; the ROADMAP tracks it.

Input has two interchangeable backends behind one surface, picked once per
process:

| backend | speaks | consent | where it works |
|---|---|---|---|
| `mutter` (default when present) | `org.gnome.Mutter.RemoteDesktop` | none | GNOME |
| `portal` | `org.freedesktop.portal.RemoteDesktop` + `ScreenCast` | one dialog, then a saved `restore_token` | GNOME, KDE, wlroots |

The pick is automatic, mutter's private API when it answers on the session
bus, the portal otherwise, and `WCU_INPUT_BACKEND=mutter|portal` forces
either for testing. Everything above input (windows, AT-SPI, capture, guards,
`do_steps`) is unchanged by the choice.

Proven on GNOME 50 Wayland, 2026-08-24: consent approved once, then absolute
motion, clicks and keysym typing all land through the portal, with the
compositor independently confirming the pointer position. The second run
reused the saved token and reached a working session in **1.0 s with no
dialog at all**, which is what makes the portal path usable unattended.

Two things worth knowing before relying on it:

* **Absolute coordinates are per-stream.** Portal absolute motion is defined
  relative to a ScreenCast stream, not the desktop, so the backend opens a
  ScreenCast session alongside the RemoteDesktop one (nothing consumes the
  frames) and maps every (x, y) into the stream that contains it. Without that
  link the portal answers `Invalid position`, the one wire the 2026-08-23
  spike left open.
* **Consent granted without input is sticky, so it self-heals.** Approving the
  dialog with *Allow Remote Interaction* switched OFF yields a session that may
  capture but never click, and the saved token would restore that forever. The
  first `Notify*` refusal discards the token and says exactly what to switch
  on, so the next action asks again.

Approving the dialog is itself automatable: the switch is a `switch` node with
a `Toggle` action, and **Share** is a `button` node, both reachable with
`ui_find` + `ui_press`. Note the roles: filtering for `push button` finds
nothing, and clicking Share by coordinate is unreliable.
