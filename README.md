# Deskwright

[![CI](https://github.com/tristanmuzzu/deskwright/actions/workflows/ci.yml/badge.svg)](https://github.com/tristanmuzzu/deskwright/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/deskwright.svg)](https://pypi.org/project/deskwright/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![GNOME Shell](https://img.shields.io/badge/GNOME%20Shell-48--50-4a86cf)](#requirements)
[![Wayland](https://img.shields.io/badge/Wayland-native-blue)](#how-it-actually-works)

**Computer use for AI agents on GNOME Wayland.**

Let your coding agent use your Linux desktop. Or give it one of its own, so it
stops stealing your mouse.

[Install](#install) &nbsp;•&nbsp;
[What it can do](#what-it-can-do) &nbsp;•&nbsp;
[A desktop of its own](#a-second-desktop-it-uses-while-you-work) &nbsp;•&nbsp;
[When something's off](#when-somethings-off) &nbsp;•&nbsp;
[Security](SECURITY.md)

<!-- demo.gif goes here: split screen, you typing on the left, the agent
     driving apps on a virtual monitor on the right, uninterrupted. -->

Claude Code can edit your files and run your commands. It can't open GIMP,
click a button in a settings dialog, or read what a native app is showing you.
On macOS and Windows it can, through computer use. On Linux that's still on
Anthropic's list.

This fills the gap on GNOME. It's an MCP server plus a small shell extension,
and it hands an agent the actual desktop: launching apps, reading widgets,
clicking, typing, dragging, window management, OCR and screen recording. Any
MCP client can drive it. Claude Code, Codex, Cursor, your own script.

The part people tend to like most: `DESKWRIGHT_SESSION=headless` runs all of
it on a virtual monitor that isn't on any of your screens. Your agent gets a real GNOME
desktop to work on, and it never takes your focus.

## Why this exists

Wayland deliberately stops an application from seeing or touching any other
window. That's a good rule, and it's why `xdotool`, `wmctrl` and `grim` either
do nothing on GNOME or fail in ways that look like your own mistake. Every
Linux computer-use project I found either shipped a Docker container running
X11, or quietly assumed X11 and broke.

So I went looking for what GNOME actually permits, and it turns out to be quite
a lot, just not where anyone looks. Mutter answers D-Bus. AT-SPI, the
accessibility layer built for screen readers, exposes every real widget in
every running app with its name, its role and the action it performs. And a
shell extension runs inside gnome-shell itself, where the rest lives.

That middle one matters more than it sounds. Pressing a widget's own
accessibility action isn't a nicer way to click. It's a different thing: it
can't miss, it survives the window moving, and it needs no pointer at all. An
agent working this way stops guessing at coordinates, and stops taking a
screenshot after every action to find out what happened.

## Install

You need GNOME Shell on Wayland. Check with `echo $XDG_CURRENT_DESKTOP
$XDG_SESSION_TYPE`, which should mention GNOME and wayland.

### The lazy way: hand it to your agent

Open Claude Code, or Codex, or whatever you use, and say:

> Set up https://github.com/tristanmuzzu/deskwright on this machine,
> follow the AGENTS.md.

[`AGENTS.md`](AGENTS.md) is a runbook written for agents. Every command, how to
check each one worked, the right package names for Debian, Fedora and Arch, and
the handful of things that go wrong with their fixes. Your agent will ask you
for a sudo password once and tell you to log out once. That's your whole
involvement.

### By hand: two commands and a logout

```bash
pipx install --system-site-packages deskwright
deskwright-setup
```

`deskwright-setup` narrates every step. It turns on the accessibility flag, installs
the bundled shell extension, and tells you about any missing system package
with the right install line for your distro, so you're never guessing at
package names. It never runs sudo itself.

**Then log out and log back in.** Once. There's no way around this one: on
Wayland, gnome-shell only picks up an extension at session start.

Last step, point your client at it:

```bash
claude mcp add deskwright --scope user -- deskwright
```

That's it. Ask your agent to open an app and it will.

<details>
<summary><b>Why <code>--system-site-packages</code>, and what breaks without it</b></summary>

PyGObject publishes no wheels to PyPI. It's a distro package everywhere
(`python3-gi`, `python3-gobject`, `python-gobject`), so an isolated venv can't
import it. Leave the flag off and you get a server that starts cleanly and then
dies on the first click with `input_backend_failed`.

`uvx` has no equivalent flag, which is the only reason this says pipx. If you'd
rather skip pipx, a plain `python3 -m venv --system-site-packages` followed by
`pip install deskwright` works identically.
</details>

<details>
<summary><b>As a Claude Code plugin instead</b></summary>

The repo is also a plugin marketplace. It registers the server plus a skill
that teaches an agent which tool to reach for first.

```bash
claude plugin marketplace add tristanmuzzu/deskwright
claude plugin install deskwright@deskwright
```

The plugin runs the server from its own checkout, so there's no pip install.
You still need the system packages and the extension, so run the setup out of
the checkout Claude Code cloned for you, once:

```bash
~/.claude/plugins/marketplaces/deskwright/bin/deskwright-setup
```

Then log out and back in, same as above.
</details>

<details>
<summary><b>From a clone, if you want to hack on it</b></summary>

```bash
git clone https://github.com/tristanmuzzu/deskwright
cd deskwright
bin/deskwright-setup
./mcp_server.py --self-test
```

`./mcp_server.py` is the same entry point as the `deskwright`
command, by the path older registrations already point at.
[`CONTRIBUTING.md`](CONTRIBUTING.md) has the layout and which test suites need
a real session.
</details>

## Check it works

```bash
DESKWRIGHT_SESSION=headless deskwright --self-test
```

You want `18/18 passed`. It runs on a virtual monitor rather than your screen,
so it's safe to run while you're working. The first run takes about 20 seconds
because it has to start a second gnome-shell.

Drop `DESKWRIGHT_SESSION=headless` and it tests your real desktop instead. Do that one
while you're looking at the screen: the self-test injects real input, because
it's checking the guards that refuse dangerous key combinations.

When something's wrong, ask your agent to call `desktop_health`. It answers in
one line whether this desktop is usable, then says which mechanisms work right
now and what each of them will actually do.

## What it can do

33 tools, roughly 11k tokens of schema in a session. That's the honest price,
and it's why each one returns enough that you don't need a second call to work
out what happened. The order below is the order an agent should reach for them.
Accessibility tree first, pixels last.

| Tool | What it's for |
|---|---|
| `ui_apps`, `ui_tree`, `ui_find` | Find things. `ui_find` searches 30 levels deep by default, because GTK4 nests far deeper than you'd expect. |
| `ui_press` | **The good one.** Invokes the widget's own action, so it can't miss. Wants `expect_name` or `expect_role`, which is the identity check, not ceremony. |
| `ui_set_text` | **The good one for typing.** Writes straight into the widget with no focus and no keyboard, then reads it back to prove the write landed. |
| `ui_read_text` | Read a widget's contents. This is how you verify something worked. |
| `launch_app` | Start an app by desktop id and wait for its window, inside one call. |
| `screen_map` | Where everything is, in pixels: windows top of stack first, plus every pressable widget of the focused app with the point to click it at. Each carries a `ref: N` you pass straight to `ui_press` or `pointer_click`. No coordinates to copy, identity re-checked on use. |
| `pointer_click`, `pointer_move`, `pointer_drag`, `pointer_scroll` | Real pointer input in absolute screen coordinates. Pass `expect_window` and a click that would land somewhere else is refused, with the blocker named so you can redirect in the same call. |
| `window_at`, `pointer_position` | What a click at a point would hit, before you click it. And where the pointer is now, or an honest note that only the last position it set is known. |
| `find_text` | Where a visible string is, in screen coordinates. OCR, about 0.3s for a window, and no image in your transcript. This is the answer for Chrome, Electron and Qt, which expose almost nothing to `ui_find`. |
| `wait_for` | Wait for a window, a widget, some text, a focus change or the clipboard, instead of sleeping a guessed number of seconds. |
| `region_changed` | Wait for pixels to change. For what `wait_for` can't express, like a reply arriving or a spinner stopping. |
| `assert_state` | Pass or fail with evidence, so a long run can decide for itself that it's finished. |
| `do_steps` | A known sequence in one call, validated before anything runs, with per-step retry and one picture at the end, or at the step that broke. |
| `list_windows`, `activate_window`, `window_manage` | Window list, focus, and move, resize, close, minimize, maximize, workspace. |
| `screenshot`, `zoom` | A picture, or a full-resolution crop of one window, region or widget. `zoom` never scales, so small text stays readable. |
| `screencast`, `frames` | For anything that moves, because a still can't show motion. `frames` also reports a per-frame delta series, which is how you tell a smooth scroll from a juddering one. |
| `type_text`, `press_keys`, `hold_key` | Keyboard input through compositor keysyms, with focus proven before anything gets typed. |
| `clipboard_read`, `clipboard_write` | Pasting beats two thousand keystrokes, and reading back is how you check it arrived. |
| `journal` | The trail of everything the agent did: arguments, outcome, whether it landed, screenshot hashes. For reviewing an unattended run, or working out where you are after a context reset. |
| `desktop_health` | One line on whether this desktop is usable, then the detail. |

## A second desktop it uses while you work

An agent that needs your screen is only half useful. `deskwright-headless` starts a
separate GNOME session on a virtual monitor, with its own session bus, its own
`gnome-shell --headless` and its own runtime directory. A server pinned to it
drives that desktop with the same 33 tools while you keep the physical one.

```bash
deskwright-headless start                    # about 200 MB of gnome-shell, idempotent
deskwright-headless status                   # liveness, memory, bus address
deskwright-headless stop                     # don't leave it idling on an 8 GB machine
```

Register it as a second MCP server and you can hand it long jobs:

```bash
claude mcp add deskwright-headless --scope user --env DESKWRIGHT_SESSION=headless -- deskwright
```

Sessions are named, so two agents can each have a desktop of their own and
never watch each other's windows move:

```bash
deskwright-headless start --name work
deskwright-headless list                     # every session, memory used, memory free
DESKWRIGHT_SESSION=headless:work deskwright
```

There are guards on this, because each session is a real compositor at around
205 MB: a per-name start lock so two agents can't both spawn one, a session cap
(`DESKWRIGHT_HEADLESS_MAX`, default 4), and a free-memory floor that refuses a start
which would push the machine into swap. The agent that would cause that can't
see it coming, so the server does.

## When something's off

**The extension says INACTIVE, or window tools don't work.** You haven't logged
out yet. gnome-shell can't load an extension without a session restart and
there's no workaround. Until then you still get AT-SPI, pointer, keyboard and
clipboard. You don't get window management, extension screenshots, pointer
position or the halt switch.

**An app shows no widgets in `ui_tree`.** It was already running when
`toolkit-accessibility` got turned on. Apps read that setting at startup, so
restart the app.

**Chrome, Electron or Qt apps look empty.** As far as AT-SPI is concerned, they
are. Use `find_text` instead, which OCRs the screen and hands back coordinates.
For Chrome specifically, launching it with `--force-renderer-accessibility`
gets you a real tree at a small performance cost.

**Everything fails with `input_backend_failed`.** pipx without
`--system-site-packages`. Reinstall with the flag:

```bash
pipx install --force --system-site-packages deskwright
```

**Every `ui_*` call says "Namespace Atspi not available".** You have
`python3-gi` but not the AT-SPI typelib, which is a separate package:
`gir1.2-atspi-2.0` on Debian and Ubuntu, `at-spi2-core` on Fedora and Arch.
`deskwright-setup --check` catches this and names it.

**A tool returns `halted`.** Somebody pressed `Super+Ctrl+Escape`, which is the
halt switch. Press it again to clear it.

**Your screen locked and half the tools stopped.** Expected. GNOME unloads
extensions that don't declare `unlock-dialog`, so screenshots and window
geometry go away until you unlock. AT-SPI keeps working, so `ui_find`,
`ui_press` and `ui_read_text` all still do.

## What it won't do

Deliberately out of scope as a project, not just off by default:

- **No CAPTCHA solving.** CAPTCHAs exist to tell humans from software. This is
  software.
- **No credential typing features.** Nothing here is built to harvest, store or
  autofill secrets. The journal doesn't record what gets typed, only how much.
- **No detection evasion.** No timing jitter to look human, no fingerprint
  spoofing, no anti-anti-bot work.
- **No cloud, no telemetry, no account.** It's a local process on your session
  bus, and nothing leaves the machine.

On the other side of that line the design is deliberately permissive. The agent
is trusted, and the tooling's job is to make it capable rather than to fence it
in. The whole safety budget goes to three things: a halt switch a human can
always reach, guards against actions that can't be undone, and an honest record
of what happened. [SECURITY.md](SECURITY.md) is blunt about what enabling this
actually switches on, and it's worth reading before you point it at your real
screen.

## How it actually works

Wayland denies all of this to Wayland clients. It says nothing about D-Bus, and
that's where the doors are. Four mechanisms, roughly in order of how much work
they carry:

1. **AT-SPI** for anything semantic. Real widgets, real actions, no pointer.
2. **A gnome-shell extension** over D-Bus for what gnome-shell keeps to itself:
   window enumeration and control, screenshots, pointer position, and a
   keybinding grab for the halt switch.
3. **`org.gnome.Mutter.RemoteDesktop`** for pointer and keyboard. Absolute
   coordinates, and keysyms rather than key positions, so your keyboard layout
   can't transpose what gets typed.
4. **`xdg-desktop-portal`** for the same thing, standardised. This is the route
   to compositors that aren't GNOME.

Worth knowing: number 2 is the only one of those that isn't already open.
Mutter's ScreenCast and RemoteDesktop interfaces answer any client on your
session bus with no consent dialog, which is why recording here needs no
permission popup, and why the portal is a caller of them rather than a gate in
front of them. [SECURITY.md](SECURITY.md) has the exact commands if you'd
rather check that yourself than take my word for it.

[`docs/field-notes.md`](docs/field-notes.md) is the long version: every wall
hit on the way here, what the compositor refused, and the measurements that
shaped the tool surface. It's the file to read when something behaves oddly, or
before you change the code.

## Requirements

`deskwright-setup --check` is the real answer. It detects everything, names the
package for your distro and exits nonzero if a hard requirement is missing.
The short version:

| Platform | Status |
|---|---|
| GNOME Shell 50 on Wayland | **Verified.** Developed on 50.1 (Ubuntu 26.04), live suites run against it on every change. |
| GNOME Shell 48 to 49 | **Should work.** Same D-Bus and AT-SPI surfaces, not tested. Reports welcome, attach `desktop_health` output. |
| KDE Plasma, Sway, Hyprland | **Input only.** The portal backend drives pointer and keyboard, but window management and screenshots need per-compositor work that isn't done. `desktop_health` will say it's not usable, and it means it. |
| X11 | Not a target. `xdotool` already does this well there. |

Packages, using Debian names (`deskwright-setup` prints yours): `python3-gi`,
`gir1.2-atspi-2.0`, `python3-pil`, `libglib2.0-bin`, `wl-clipboard`,
`tesseract-ocr`. `ydotool` is optional and only used as an input fallback. The
headless session additionally wants `gnome-shell` and `dbus-daemon` as
binaries, which any normal desktop already has.

## Contributing, security, license

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the layout, the test suites and what
each one needs. [`SECURITY.md`](SECURITY.md) is the threat model, stated
plainly. [`ROADMAP.md`](ROADMAP.md) is what's next and why, in order.

Apache-2.0. If you get it running on a compositor that isn't GNOME, or on a
GNOME older than 50, please open an issue and say so. That's the most useful
thing anyone can send.
