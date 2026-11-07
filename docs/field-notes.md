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
tools are built on it; `deskwright/atspi_ui.py` is the standalone CLI for poking at it by
hand:

```bash
deskwright-atspi apps
deskwright-atspi tree "Google Chrome" --depth 6
deskwright-atspi find "Reload" --role "push button"
deskwright-atspi actions "gnome-tweaks/0"
deskwright-atspi do "gnome-tweaks/0" 0
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
(`deskwright/extension/deskwright@zeticle.com`, D-Bus name `com.zeticle.deskwright`). An
extension runs inside gnome-shell, so the calls the compositor refuses to a
client are ordinary calls to it. `deskwright/desktop.py` is the standalone CLI over the
same mechanisms:

```bash
deskwright-desktop windows
deskwright-desktop activate <id>
deskwright-desktop screenshot shot.png
deskwright-desktop type "hello"
deskwright-desktop key ctrl+s
```

Keystrokes from `deskwright/desktop.py` go through `ydotool` and `/dev/uinput`, below the
compositor. This is **focus-blind**: it types wherever focus happens to be, so
call `activate` first, and it is also **layout-blind**: ydotool sends
US-QWERTY keycodes and the compositor maps them through the active layout, so
on a `de` layout a typed `y` arrives as `z`. The MCP server does not have this
problem; it sends keysyms through the compositor instead.

### One combination is refused outright

`Ctrl+Alt+F1` … `F12` is `switch-to-session` in mutter. Injecting one throws the
desktop onto a different virtual terminal showing a login screen, which is
indistinguishable from a frozen machine. It cost a session and a hard
power-off on 2026-08-08. `deskwright-desktop key` and the server both refuse it rather
than trusting the caller to remember.

## The MCP server, the way this is actually meant to be used

The CLIs above are for poking at things by hand. In a session, use the MCP
server: registered at user scope, "do this on my laptop" works without
remembering a script path.

An installed copy proves itself with one command, on a desktop you cannot see:

```bash
DESKWRIGHT_SESSION=headless deskwright --self-test
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
`deskwright/remote_input.py`), which takes absolute coordinates in the same space
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
bus, the portal otherwise, and `DESKWRIGHT_INPUT_BACKEND=mutter|portal` forces
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


## No-op window layout verification still waits (2026-09-08)

In the local 71-action approval trial, requesting geometry that already matched
the window took about two seconds of backend time. The window-management loop
waits for geometry to change rather than recognizing an already satisfied target.
The new restricted window_layout tool inherits that same implementation. This
latency issue was left unchanged in both conditions to isolate scoped approval
changes. A follow-up should validate the requested geometry and state before
waiting, while retaining verification for actual moves and asynchronous mapping.

## Semantic GUI research findings (2026-10-06)

The [research report](semantic-gui-research.md) and its raw samples document
successful semantic document/form work and current limits. Two points affect
future implementation: GTK4 editor controls reported ENABLED=false while
SENSITIVE=true and usable; a Chrome semantic action ran while a GNOME keyring
prompt covered the private desktop. Compositor hit-testing identified no app
receiver at the prompt. Native accessibility identity is not proof of physical
reachability or permission to act through a modal. No credentials were entered.

Unrelated verification failures present in the starting dirty worktree were
left for the guidance change that owns them: `tests/test_prose.py` rejected a
curly apostrophe in `skills/deskwright-codex/SKILL.md:26` and still expected the
old inline runbook commands in `AGENTS.md`; Ruff flagged import ordering at
lines 10 and 34 of `skills/deskwright-codex/scripts/private_server.py`. The
research run had 390 passing tests and those two failures. No production
runtime code was modified by this investigation.


## Semantic interaction hardening (2026-10-06)

The follow-up implementation adds `ui_snapshot`, `ui_inspect`, `ui_action` and
`ui_wait`; see [the execution contract and live evidence](semantic-interaction.md).
It resolves the researched shell-modal gap for the new action API by requiring
GNOME `InteractionState` (extension build `2026-10-06.1`). In a private live
session, an actual Run dialog held a modal grab while the underlying form still
reported focused. Invocation refused with `occluded` / `not_started`; exact form
readback confirmed no change. Legacy tools retain compatibility behavior.

Two provider details were discovered during hardening. GTK4 frame nodes expose
more than 32 actions, so rejecting a large action table incorrectly hid their
otherwise useful descendants. Snapshots now cap the listed actions per node and
report truncation while continuing the traversal. Editing also adds undo actions
and shifts action indices: invocation rechecks the action table, while text
readback validates native object/name/role independently of those indices.

The visible main-desktop demo filled 14 fields and three options in 3.160 seconds;
its clear/reopen/exact verification phase took 0.408 seconds. Five final private
runs through the hardened API all passed, with a median 0.665-second fill phase
and 1.434-second complete workflow, including readback and deliberate refusals.
These are worker-local timings on a controlled form, not a model benchmark or
an end-to-end speed ratio. An initially premature disk read after Save confirmed
that native acceptance must be followed by an application completion predicate.

Final verification: 437 tests passed, with the same two pre-existing guidance
failures listed above; the private real-session self-test passed 18/18. Changed
Python files passed Ruff; whole-tree Ruff still reports only the two pre-existing
launcher import-order findings. The edited extension passed the GJS parser and
was loaded only into a newly started private desktop. The tested extension file was staged in the installed directory with a backup;
main-desktop activation still requires the next normal login and an MCP reconnect.
No user logout was done. Both owned main-demo windows were closed, and the owned
private desktop was stopped after verification.

## Main-desktop real-application comparison (2026-10-07)

After the user's logout/login, extension build 2026-10-06.1 and all four semantic
tools were live on the main desktop. The [real-app comparison](semantic-real-app-comparison.md)
completed Files → Text Editor → Calculator → Files with identical verified files
in both conditions. Semantic-first with fallbacks took 582.06 seconds; ordinary
screenshot/input took 202.69 seconds. Backend totals were about 20.55 seconds each.
This single ordered trial includes discovery and warm-start bias, not evidence of
a semantic speedup. No production code was changed between conditions.

Confirmed follow-ups: fresh references inside Files' embedded New Folder dialog
fail the outer-frame versus nearest-dialog identity comparison; unnamed popups
are missing from the parent snapshot; file rows lack open/select operations;
inherited actions flood broad snapshots. Calculator also showed that accepted
invocation needs a later completion check. Legacy type_text verification rejected
successful shared-prefix path replacement and Calculator's `*` → `×` conversion.
The comparison report records the recovery and remaining work. Preserve guards
while fixing these cases; do not turn false refusals into unverified success.

## Hybrid speed and reliability follow-up (2026-10-07)

The [semantic contract](semantic-interaction.md#hybrid-improvements-and-verification-2026-10-07)
records the measured fixes: selection-aware typing, pinned-object verification,
8 ms keysym default, optional compact snapshots, GTK4 embedded-dialog and GTK3
portal-root identity, and readiness/changed-text waits. Three prepared real-app
workflows passed exactly in 8.133/7.743/7.733 seconds, excluding model decisions.
No extension changes or user-desktop input were needed in this follow-up.

GTK's text selection must use `Atspi.Text.get_selection(node, index)`:
`Accessible.get_selection()` names the different selection interface and takes
no index. Unit fakes initially hid that mismatch; the live Files test caught it.

The private compositor dropped accented, CJK and em-dash keysyms at the original
20 ms typing delay. Faster typing did not cause that failure. Exact native
Unicode replacement passed. Preserve the failure and use the native route;
do not silently substitute or replay input. This underlying keymap/input issue
remains outside the implemented verification fix.

Only comparison-owned processes/windows were cleaned up. Two retained private
editor processes stopped responding to close after save-dialog/restore state and
were terminated by their exact owned PIDs; no main-desktop process was touched. Future
benchmark repetitions use explicit new-window launch and verify window closure.

## 2026-10-07: exact queries and end-to-end batching

The [final query comparison](semantic-real-app-comparison.md#exact-queries-and-shorter-agent-batches-2026-10-07)
used complete, unambiguous multi-control observations and disappearance predicates
inside short agent batches. Both final main-desktop pairs completed about 48%
faster than the visual baseline, with exact artifacts and no tool exceptions.
Backend operations themselves were slightly slower; model/transport round trips
dominated. Keep backend timings separate from full agent completion time.

A query must use its remaining timeout as the scan budget. A separate 1.5-second
cap discarded partial scans on a busy real desktop, causing avoidable timeouts.
The five-second default remains a maximum, not a fixed wait. Incomplete traversal
still cannot prove uniqueness or disappearance.

Confirmed setup defect, outside this change: a long private session name can
make the combined Wayland socket path exceed Linux's 108-byte Unix socket limit.
The failed test name `deskwright-query-live-_fg4lmp6` produced this exact Mutter
error. A shorter unique name started successfully. No setup-code change was made.

## 2026-10-07: varied hybrid tasks expose navigation and targeting costs

The [four varied tasks](semantic-real-app-comparison.md#four-varied-hybrid-tasks-2026-10-07)
all produced exact outputs, but averaged 238.38 seconds with a 148.83–410.13 second
range. Nine interrupted tool batches and one recovered approval block remain in
the measurements. This does not support a broad, consistently large speedup claim.

Confirmed application behavior: Archive Manager's Extract chooser accepted and
read back a changed Location string, but Extract still used Home without
committed navigation. The two synthetic archive files were identified by exact
archive bytes and selected native rows, then moved through Files to the fixture;
none remained in Home. Native text replacement is not a navigation-completion
contract. A destination check is needed before submitting this dialog.

Other observed costs: the save button reads Replace while the name already
exists; Papers uses window class `papers`; full paths in Nautilus' File Name
field did not complete a save in this trial. Four complete-tree query attempts
timed out, including a disappearance check after successful folder creation.
Use the recorded actual controls and bounded fallback; do not infer failed input
from a failed observation. These are recorded follow-ups, not fixes verified in
this turn. Production code stayed frozen for all four runs.

## 2026-10-07: general hybrid cost routing and recovery

See the [component measurements and live limits](semantic-real-app-comparison.md#general-hybrid-routing-and-recovery-2026-10-07).
Observed subtree queries reduced median backend lookup time by 88.3% on a dense
fixture. Skipping repeated unproductive 200 ms probes reduced warm observation
cost by 51.7%. These exclude model time and do not establish end-to-end superiority.
Only route costs are cached; controls and postcondition success are never cached.

Newly announced windows can have zero geometry. An attempted zero-size crop
failed twice; `ui_observe` now captures the full desktop during mapping. Its native
traversal budget remains cooperative: a cold AT-SPI handshake took 5.70 seconds.
Do not describe the scan budget as a hard per-call latency guarantee.

Loupe exposes native toolbar controls without exposing its displayed image as a
drawing area. Explicit visual observation remains necessary for image/layout
work. GNOME Text Editor's native buffer omits its implicit final newline; saved
artifact bytes and native text need separate explicit expectations.

Papers exited with status 1 and `Failed to open display` in the private test
session while the other apps worked. General direct-launch polling now reports
an unsuccessful exit early, with PID/status and no replay. This reduced that
failed call from 15.75 to 1.57 seconds including capture, but the application-level
display failure remains unresolved and no claim of private Papers coverage is made.

Existing verification failures outside these changes: `tests/test_prose.py`
rejects a curly apostrophe in the already modified private-desktop skill and still
expects setup commands inline in AGENTS.md after their move to the linked runbook.
Repository-wide Ruff reports two import-order violations in the existing
`skills/deskwright-codex/scripts/private_server.py`. These unrelated files were
preserved; changed implementation and test files pass Ruff.

## 2026-10-07: autonomous provider, document and recovery cycle

The earlier prose and import-order failures are now repaired narrowly: the prose
test follows the linked setup runbook, the skill uses an ASCII apostrophe, and
the private launcher imports are sorted. Full suite: 527 passed; Ruff is clean.

Native Collection queries greatly reduced Writer lookup work, but their first
implementation stalled on Calc's virtual cell tree. A small result limit bounds
matching results, not work spent searching for rare roles. A capped all-node
probe now rejects oversized scopes before role filtering. Calc remains a visual
or narrowly scoped workflow; a whole-window native query does not establish
completeness. Do not infer a missing button from its timeout.

Refreshing only the target widget was insufficient: a cached ancestor could
still point to its former window. A reproduced mock accepted one action before
the fix and zero afterward. Roots and ancestor relationships now refresh before
identity and scope checks. No unsafe state/result cache was introduced.

Long normal-type startup windows still occur after the two-poll mapping check.
GIMP's first ID was replaced; LibreOffice's document populated under a temporary
title. Fresh window discovery and document postconditions handled both. Arrival
is not readiness, and adding an application-name sleep would not solve that.

Papers' private-display failure is an AppArmor restriction: its connection to the
custom private Wayland socket returned EACCES, while its installed policy allows
the usual wayland-[0-9] socket paths. Policy was not weakened or bypassed.

See the [autonomous comparison](semantic-real-app-comparison.md#autonomous-improvement-cycle-2026-10-07)
for final repeated timings, exact artifact checks, canvas recovery and limits.
