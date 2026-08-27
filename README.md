# wayland-computer-use

**Native computer use for AI agents on GNOME Wayland.** Nothing mainstream
covers this ground: Anthropic's reference computer-use environment is a Docker
container running X11, and the desktop-automation stories of Claude Code and
Codex skip Linux entirely. Meanwhile the classic Linux automation tools —
`xdotool`, `wmctrl`, `grim` — are X11- or wlroots-only and fail on GNOME in
ways that look like your own mistake. This project is an MCP server plus a
small GNOME Shell extension that together give an agent the real desktop:
screenshots and recordings, the accessibility tree, compositor-performed
pointer and keyboard input, window management, OCR and clipboard — with every
action verified and its result shown, because the agent's round trips, not the
work, are the cost.

## Support matrix

| Platform | Status |
|---|---|
| GNOME Shell 48–50 on Wayland | **Supported.** Developed and continuously verified on GNOME 50.1 (Ubuntu 26.04). |
| KDE Plasma (Wayland) | Planned, via an xdg-desktop-portal backend (see [ROADMAP](ROADMAP.md)). |
| wlroots compositors (Sway, Hyprland) | Planned, same portal backend. |
| X11 sessions | Not a target — mature alternatives already exist there. |

## Install

```bash
git clone https://github.com/wayland-computer-use/wayland-computer-use
cd wayland-computer-use
bin/wcu-setup
```

`wcu-setup` enables the accessibility toolkit flag, installs the bundled GNOME
Shell extension (`extension/wcu@wayland-computer-use` — one logout/login is
required before the shell loads it; the setup says so), optionally sets up the
`ydotoold` fallback, and prints the `claude mcp add` / `.mcp.json` snippet for
your client. For Claude Code specifically — including the recommended
auto-approval allowlist that makes unattended runs possible — see
[docs/claude-code-setup.md](docs/claude-code-setup.md).

Then prove it works:

```bash
./mcp_server.py --self-test
```

## The tools

The server exposes **33 tools**. The ordering below is the ordering an agent
should prefer — the accessibility tree first, pixels last:

| Tool | Notes |
|---|---|
| `ui_apps`, `ui_tree`, `ui_find` | Locate things. `ui_find` searches to depth 30 by default — see [GTK4 nests deeper than you think](#gtk4-nests-deeper-than-you-think). |
| `ui_read_text` | Read a text widget's content. This is how you *verify* something landed. |
| `ui_set_text` | **Preferred text entry.** AT-SPI `EditableText`: no focus, no keyboard, and it reads the widget back to prove the write. |
| `ui_press` | **Preferred action.** Invokes the widget's own action. Requires `expect_name`/`expect_role`. |
| `launch_app` | Start an application by desktop id and wait for its window, inside the protocol. |
| `screen_map` | Where everything is, in pixels: windows top of the stack first with their centres, and every pressable widget of the focused app with the point to click it at. Each widget carries a `ref: N` — pass it straight to `ui_press(ref)` or `pointer_click(ref)`, no coordinates, identity re-checked, refs die at the next `screen_map`. |
| `window_at` | What a click at a point would hit, *before* clicking it. |
| `pointer_click`, `pointer_move`, `pointer_drag`, `pointer_scroll` | Real pointer input at absolute screen coordinates. Pass `expect_window` and a click that would land elsewhere is refused — naming the blocker's id and geometry, so `on_occluded: "click_topmost"` can redirect in the same call. `hover_first` for CEF/Electron buttons that ignore a click with no preceding motion. |
| `pointer_position` | Where the pointer is, or an honest statement that only the last set position is known. |
| `find_text` | **Where a visible string is, in screen coordinates.** OCR. This is the answer for Chrome, Electron and Qt, which expose almost nothing to `ui_find`. ~0.3 s for a window, and no image in the transcript. |
| `wait_for` | Wait for `window_exists` / `window_gone` / `window_focused` / `focus_changes` / `text_appears` / `widget_exists` / `clipboard_changed` / `elapsed` instead of sleeping a guessed number of seconds. A timeout over 300 s is clamped and reported, not refused. |
| `region_changed` | Wait for *pixels* to change — a reply arriving, a spinner finishing — for the things `wait_for` cannot express. |
| `assert_state` | Prove completion: pass/fail with evidence, so a run can end itself. |
| `do_steps` | A known sequence of actions in ONE call, validated up front, with per-step retry policy and one picture at the end or at the step that failed. |
| `list_windows`, `activate_window`, `window_manage` | Extension-backed window list, focus, and move/resize/close/minimize/maximize/workspace verbs. |
| `zoom` | A full-resolution crop by window, region or widget path — "look closer" as a first-class verb, never scaled. |
| `screenshot` | With `annotate`, `window` and `region` cropping. |
| `screencast`, `frames` | For anything that moves. Stills cannot show motion. `frames` reports a per-frame delta series and a jerk figure, because peak and mean cannot tell a smooth pan from a jolting one. |
| `type_text`, `press_keys`, `hold_key` | Compositor keysyms by default, focus proven first, `ydotool` only as a fallback. |
| `clipboard_read`, `clipboard_write` | Paste beats two thousand keystrokes; reading back is a verification primitive. |
| `journal` | Read back the trail of acted tool calls — arguments, outcome, hit/miss verdict and screenshot hash — to review an unattended run or reconstruct state after context loss. |
| `desktop_health` | A one-line verdict first — READY / usable-with-limits / not usable, and which desktop it is talking about — then which mechanisms are usable right now, what each will actually do, and which extension methods the *running* shell has. |

## Design philosophy

**The agent is trusted; the tooling's job is to make it capable and
self-sufficient, not to fence it in.** The server never blocks a capability
the platform allows. The safety budget goes to exactly three things:

1. **A kill switch a human can always reach.** `Super+Ctrl+Escape` is grabbed
   by the extension inside the compositor (where client grabs are refused),
   halts every state-changing tool, and cannot be pressed or cleared by
   injected input.
2. **Irreversibility backstops.** Guards so one bad action cannot destroy a
   machine or an account: clicks are refused when they would land in the wrong
   window, widgets are re-checked before pressing, focus is proven before
   typing, and the session-switch key combination is refused outright. A
   tripwire for genuinely unrecoverable patterns warns — it does not ask a
   human who is not looking at the screen.
3. **Honest evidence.** Every acting tool reports whether it landed; GNOME's
   screen-share indicator is visible whenever the pointer session is open; the
   goal is that the user can always see what actually happened, after the
   fact, instead of supervising during.

Everything else is the agent's judgment. Restrictive policy — per-app tiers,
redacted surfaces, view-only modes — is planned only as **opt-in
configuration** for deployments that want it, defaulting to off. Every round
trip the agent does *not* need — every question it does not have to ask, every
screenshot it does not have to request, every retry it can decide alone — is
the product.

## Scope: what this project will not do

Deliberately out of scope, as a project and not merely as a default:

- **No CAPTCHA solving.** CAPTCHAs exist to distinguish humans from software;
  this is software.
- **No credential typing features.** Nothing here is designed to harvest,
  store, or enter passwords, payment details, or tokens on a user's behalf.
- **No detection evasion.** The screen-share indicator stays visible, input
  goes through sanctioned compositor APIs, and there are no features for
  making automation look like a human to software trying to tell the
  difference.

Honest positioning beats fencing: the audience for an autonomy-first tool is
exactly the audience that checks.

---

# Field notes

Everything below is the lab notebook this project grew out of: what was tried,
what the compositor refused, and what was measured. The measurements are the
reason the tools are shaped the way they are. Dates are retained because each
finding was true of a specific GNOME on a specific day; re-measure before
assuming drift.

The original plan assumed Xorg and `xdotool`. That is dead: Ubuntu 26.04 ships
no Xorg session and one cannot be installed, and XWayland runs X11
*applications* without giving anyone global input or window control. So this
is built on what Wayland actually permits.

## What does not work, and why

Each of these was tried first and refused. They are listed because every one of
them is documented somewhere as the way to do it.

| Approach | What happens |
|---|---|
| `xdotool` / `wmctrl` for global input or window control | Only ever sees XWayland clients. Useless for native Wayland windows. |
| `org.gnome.Shell.Screenshot` over D-Bus | `AccessDenied: Screenshot is not allowed` |
| `org.gnome.Shell.GrabAccelerator` over D-Bus | `AccessDenied: GrabAccelerator is not allowed` — this is why GNOME "custom shortcuts" configured by a script silently never fire |
| `grim` | wlroots-only; GNOME does not implement the protocol |
| Asking where the pointer is | Not permitted to any client. `cursor_position()` returns the centre of the screen forever, even under XWayland |
| A client raising itself | No protocol for it in GNOME (no `wlr-layer-shell`) |
| `ydotool mousemove --absolute` | Silently does nothing here. Relative motion works but is put through mutter's pointer acceleration, so units are not pixels: measured 2026-08-16, 5 units moved 2 px and 200 units moved off the edge of the screen |
| `xdotool click` under XWayland | Not routed to the compositor, and asking pops an `xdg-desktop-portal-gnome` "Remote Desktop / Allow Remote Interaction" dialog that grabs input until it is dismissed |
| `ReloadExtension` over D-Bus | `NotSupported: ReloadExtension is deprecated and does not work` on GNOME 50.1. An edited extension is not running until the next login, and disable/enable does not help — gnome-shell has already imported the module |

## What does work

**The accessibility tree. Start here.**
Every application exposes its real widgets with roles, names and invokable
actions. Pressing the actual button beats clicking a pixel: it cannot miss, it
cannot be defeated by a window moving, and it needs no pointer. The `ui_*`
tools are built on it; `atspi_ui.py` is the standalone CLI for poking at it by
hand:

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

**The compositor itself, via the bundled extension.**
Screenshots, the window list, focus control, window management, pointer
position and the halt switch come from the **bundled GNOME Shell extension**
(`extension/wcu@wayland-computer-use`, D-Bus name `org.wcu.Helpers`). An
extension runs inside gnome-shell, so the calls the compositor refuses to a
client are ordinary calls to it. `desktop.py` is the standalone CLI over the
same mechanisms:

```bash
./desktop.py windows
./desktop.py activate <id>
./desktop.py screenshot shot.png
./desktop.py type "hello"
./desktop.py key ctrl+s
```

Keystrokes from `desktop.py` go through `ydotool` and `/dev/uinput`, below the
compositor. This is **focus-blind** — it types wherever focus happens to be, so
call `activate` first — and it is also **layout-blind**: ydotool sends
US-QWERTY keycodes and the compositor maps them through the active layout, so
on a `de` layout a typed `y` arrives as `z`. The MCP server does not have this
problem; it sends keysyms through the compositor instead.

### One combination is refused outright

`Ctrl+Alt+F1` … `F12` is `switch-to-session` in mutter. Injecting one throws the
desktop onto a different virtual terminal showing a login screen, which is
indistinguishable from a frozen machine — it cost a session and a hard
power-off on 2026-08-08. `desktop.py key` and the server both refuse it rather
than trusting the caller to remember.

## The MCP server — the way this is actually meant to be used

The CLIs above are for poking at things by hand. In a session, use the MCP
server: registered at user scope, "do this on my laptop" works without
remembering a script path.

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
machine:

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
  entirely — OCR reads 0 words against 106 at 1568px, and a human reading it
  struggles. Crop instead: a window is ~1300 tokens and a 1200x100 strip is 160,
  against 1843 for the whole desktop, and all of them stay legible.
* **"percent of pixels changed" cannot tell a hit from a miss.** A real button
  press moves 0.05% of a window. What separates them is contrast: a miss moves
  0 cells by more than 60/255, the smallest real press moves 22.

### Pointing, and how to know where to point

The pointer goes through `org.gnome.Mutter.RemoteDesktop` (see
`remote_input.py`), which takes absolute coordinates in the same space
`list_windows` reports geometry in. No acceleration curve, no consent dialog,
no closed loop. Proven by `tests/test_pointer.py`, which puts a witness window
on screen and asserts against what it actually received.

Four ways to turn "click that button" into a number, best first:

1. `ui_press` — do not click at all. Press the widget.
2. `screen_map` — the widget list already carries `click_at` coordinates taken
   from the accessibility tree, so no measuring off an image.
3. `find_text` — OCR, returning `click_at` in screen coordinates. Slower than
   the tree and blind to icon-only buttons, but it reads Chrome, Electron and
   Qt, where the tree is empty.
4. `screenshot` with `annotate` — draws the grid, the window boxes and the
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

With the flag the whole page is exposed with real screen bounds — headings,
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
favourable — it is a worked example of the trade, not a universal verdict:

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
call fails — which looks identical to the extension being broken and has a
completely different remedy. `desktop_health` tells the two apart by reading
`session-modes` at the time it is asked.

The bundled extension lists `["user", "unlock-dialog"]`, so screenshots, the
window list, focus control and the halt switch survive a lock. That is a
deliberate choice with a cost attached: it also means the desktop can be
screenshotted while locked. Dropping `unlock-dialog` reverses both — see
[SECURITY.md](SECURITY.md).

AT-SPI is unaffected either way: `ui_find`, `ui_press`, `ui_set_text` and
`ui_read_text` keep working while locked. That is a second reason to prefer
them.

The pointer is a third case. `org.gnome.Mutter.RemoteDesktop` is mutter's, not
the extension's, so it does not care about session modes — but there is nothing
worth clicking on a lock screen, and clicking blind is exactly what the guards
exist to prevent.

### Extension changes need a logout

gnome-shell imports an extension once per session and **cannot reload it on
Wayland** — `ReloadExtension` is deprecated and disable/enable does not
re-import the module. Until the next login after an install or edit, the
server degrades honestly: `pointer_position` reports the last position it set
and says so, `window_at` falls back to window rectangles and warns that an
input-shaped overlay will fool it, and region captures are cropped
client-side. `desktop_health` lists exactly which methods the running shell
has, and the extension's `Ping` method returns the loaded build stamp.

## Two input backends: mutter, and the portal (KDE/wlroots route)

Input has two interchangeable backends behind one surface, picked once per
process:

| backend | speaks | consent | where it works |
|---|---|---|---|
| `mutter` (default when present) | `org.gnome.Mutter.RemoteDesktop` | none | GNOME |
| `portal` | `org.freedesktop.portal.RemoteDesktop` + `ScreenCast` | one dialog, then a saved `restore_token` | GNOME, KDE, wlroots |

The pick is automatic — mutter's private API when it answers on the session
bus, the portal otherwise — and `WCU_INPUT_BACKEND=mutter|portal` forces
either for testing. Everything above input (windows, AT-SPI, capture, guards,
`do_steps`) is unchanged by the choice.

Proven on GNOME 50 Wayland, 2026-08-24: consent approved once, then absolute
motion, clicks and keysym typing all land through the portal, with the
compositor independently confirming the pointer position. The second run
reused the saved token and reached a working session in **1.0 s with no
dialog at all** — which is what makes the portal path usable unattended.

Two things worth knowing before relying on it:

* **Absolute coordinates are per-stream.** Portal absolute motion is defined
  relative to a ScreenCast stream, not the desktop, so the backend opens a
  ScreenCast session alongside the RemoteDesktop one (nothing consumes the
  frames) and maps every (x, y) into the stream that contains it. Without that
  link the portal answers `Invalid position` — the one wire the 2026-08-23
  spike left open.
* **Consent granted without input is sticky, so it self-heals.** Approving the
  dialog with *Allow Remote Interaction* switched OFF yields a session that may
  capture but never click, and the saved token would restore that forever. The
  first `Notify*` refusal discards the token and says exactly what to switch
  on, so the next action asks again.

Approving the dialog is itself automatable: the switch is a `switch` node with
a `Toggle` action, and **Share** is a `button` node — both reachable with
`ui_find` + `ui_press`. Note the roles: filtering for `push button` finds
nothing, and clicking Share by coordinate is unreliable.

## The headless second session

An agent that needs the user's screen is only half autonomous. `wcu-headless`
starts a **separate GNOME session on a virtual monitor** — its own session
bus, its own `gnome-shell --headless`, its own private `XDG_RUNTIME_DIR` —
and a second server instance pinned to it drives that session with the
identical 33-tool surface while the user keeps the physical screen. Proven
end to end (launch → compositor-confirmed click → `do_steps` typing →
AT-SPI read-back → screenshot) on GNOME 50, 2026-08-24.

```bash
wcu-headless start                     # bring it up (~200 MB gnome-shell), idempotent
WCU_SESSION=headless ./mcp_server.py --self-test   # a server pinned to it (auto-starts too)
wcu-headless status                    # liveness, RSS, bus address
wcu-headless stop                      # end it -- do not leave it idling on 8 GB machines
eval "$(wcu-headless env)"             # pin a SHELL to it instead, for poking by hand
```

Register it as a second MCP server (`claude mcp add wcu-headless --scope user
--env WCU_SESSION=headless -- /path/to/mcp_server.py`) and an agent can drive
long tasks on the virtual desktop while the user works undisturbed.

### More than one of them

Sessions are **named**. Two agents that each want a desktop of their own ask
for different names and get genuinely separate compositors; the same name
means the same desktop, which is what `WCU_SESSION=headless` (the `default`
session) gave everybody before names existed.

```bash
wcu-headless start --name work         # a second desktop, alongside `default`
wcu-headless list                      # every session, total RSS, free memory
wcu-headless stop --name work          # end one; --all ends every one
WCU_SESSION=headless:work ./mcp_server.py     # a server pinned to that one
```

Everything that must not collide is derived from the name — the state file,
the `XDG_RUNTIME_DIR`, and the Wayland display. The runtime dir is the one
that bites: two sessions sharing one fight over `at-spi/bus`, last bind wins
the path, and the *other* session's apps then time out registering
accessibility. That is the 2026-08-24 incident, and suffixing per name is the
same fix applied between headless sessions.

Guards, because each session is a real compositor at ~205 MB: a per-name
start lock (two agents calling `ensure()` in the same 20 s window cannot both
spawn one), a session cap (`WCU_HEADLESS_MAX`, default 4), and a
`MemAvailable` floor that refuses a start which would push the machine into
swap — which the agent that caused it cannot see.

Proven on GNOME 50, 2026-08-27: `./tests/test_two_sessions.py`, 17/17 — two
desktops driven at once, each seeing only its own window and reading back
only its own text, with the user's primary session seeing neither and its
accessibility bus still answering.

**The filesystem is still shared.** Only the screen is separate: an app
launched on a headless session restores the user's real drafts and writes to
the user's real files. Give throwaway apps a throwaway `XDG_DATA_HOME`, as
that test does.

What makes it work: nothing in `wcu/` assumes the primary session — every
backend (extension D-Bus, AT-SPI, mutter RemoteDesktop/ScreenCast,
`gio launch`) resolves the session from `DBUS_SESSION_BUS_ADDRESS` /
`WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR` at call time, so pinning is purely an
environment operation. The bundled extension is user-scoped, so the headless
shell loads it unmodified.

Three deliberate differences from the primary session:

* **ydotool is refused** (`wrong_session`): uinput injection enters below the
  compositor on the machine's *real* seat, so it would land on the user's
  screen no matter what the environment says. The compositor routes (keysyms,
  RemoteDesktop pointer) are the only honest ones, and they are the defaults.
* **`launch_app` spawns the desktop file's `Exec` line directly** instead of
  `gio launch`: D-Bus activation on the private session proved unreliable
  (the daemon spawns the service, the window never appears), and a directly
  spawned app is its own primary instance.
* **The runtime dir is private, with the user's PipeWire symlinked in.** Two
  sessions sharing one `XDG_RUNTIME_DIR` fight over `at-spi/bus` — last bind
  wins the path and the *user's* apps time out registering accessibility.
  Found the hard way; the isolation is not optional.

**The headless session is also the CI rig.** The full e2e suite runs against
it — `WCU_SESSION=headless ./tests/test_e2e_real_task.py`, 22/22 on GNOME 50
— which retires the old constraint that live verification serializes on the
user's desktop. Every live script suite honors the same variable, because
`mcp_server` pins the session at import.

Three measured characteristics to know:

* **`screencast` records only what changes.** The virtual monitor delivers
  PipeWire frames on damage, not on a refresh clock: a still desktop yields a
  0-second file, the same capture with a pointer moving yields real frames.
  Record while the action happens (which is what a recording is for), and
  prefer `screenshot` for still evidence.
* **No XWayland** — the headless shell does not spawn it, so X11-only
  helpers (like the pointer suite's witness window) skip there.
* **Apps restore their own state.** `--standalone` gnome-text-editor still
  resurrects unsaved drafts from `~/.local/share/org.gnome.TextEditor/`, and
  a terminated test run is exactly what leaves such drafts — the e2e suite
  gives its editor a throwaway `XDG_DATA_HOME` for this reason. Agents
  launching stateful apps on the headless session inherit the user's real
  app state by design; target window IDs, not window counts.

## Requirements

- GNOME Shell 48–50 on Wayland.
- The bundled `wcu@wayland-computer-use` extension, installed and enabled
  (`bin/wcu-setup`, or see [extension/README.md](extension/README.md)). It is
  only picked up at session start; there is no way to reload gnome-shell on
  Wayland without ending the session.
- `python3-gi` and Pillow (`python3-pil`). PyGObject is what talks to
  `org.gnome.Mutter.RemoteDesktop`; Pillow does the cropping, scaling and
  annotation.
- `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
- Optional: `ydotoold` as a **system** service with its socket at
  `/run/ydotoold.socket` — only needed for the `via: "ydotool"` fallback.

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md) — module map, the test suites and which
  need a live desktop, commit style.
- [SECURITY.md](SECURITY.md) — the threat model, what the halt switch does and
  does not promise, lock-screen implications, how to report.
- [ROADMAP.md](ROADMAP.md) — where this is going, in priority order.
- License: [Apache-2.0](LICENSE).
