# Security

This document is the threat model, stated plainly. The project's design
philosophy is autonomy-first — the agent is trusted and capabilities are not
fenced — so it matters that everyone deploying it understands exactly what
they are switching on.

## What the server can do

Everything the logged-in user can. A connected client can read the screen,
record it, read the accessibility tree of every running application (including
text in password-manager-adjacent UIs that render as plain widgets), inject
pointer and keyboard input below the application layer, read and write the
clipboard, move/close/resize windows, and launch applications. Some of this —
screenshots, window queries, AT-SPI reads and writes — keeps working while the
screen is locked (see below).

There is no capability tiering, no per-app allowlist, no redaction, by
default. An opt-in policy configuration for cautious deployments is planned
(ROADMAP #45) and will default to off.

## The trust boundary is the MCP client

The server speaks MCP over stdio to whatever process launched it. It performs
no authentication and no authorization of its own: **anything that can send it
a tool call is fully trusted.** The security of a deployment is the security
of the MCP client and of the model driving it. Do not expose the server over a
network transport; do not register it in an environment where an untrusted
process can spawn it or write to its stdin.

The guards that do exist (`expect_window` on clicks, `expect_name`/
`expect_role` on widget presses, focus proven before typing, the
`Ctrl+Alt+F1`…`F12` session-switch refusal) are **correctness** guards — they
stop the agent damaging things by accident. They are not a defense against a
malicious caller, and are not presented as one.

## The halt switch, and its stated limitation

`Super+Ctrl+Escape` is a keybinding owned by the bundled GNOME Shell extension
— grabbed inside the compositor, where client grabs are refused, so no
application and no injected input can take it first. Engaging it sets a flag
the server checks before **every state-changing tool**; reading tools keep
working, so a human who has halted the agent can still see what it sees.
The extension paints a HALTED border while engaged.

Designed-in properties:

- The server refuses to inject the halt combo itself, so an agent cannot
  clear (or engage) its own halt through this server's tools.
- The extension debounces a second press within 2 seconds of engaging,
  against an injected double-press racing a human.

Stated limitations, honestly:

- **The halt switch exists only when the extension is loaded.** On a shell
  that has never loaded it — before the post-install re-login — `halt_active()`
  answers no at zero cost: a switch that was never there cannot be engaged,
  and refusing every acted call on a machine that simply has not logged out
  yet would make a fresh install useless. **Once it has answered, that
  reverses**: a probe failure after the first successful answer counts as
  *halted*, because the same `launch_app` primitive an agent uses for real
  work can run `gnome-extensions disable wcu@wayland-computer-use` and unown
  the bus name. A disappearing kill switch is a stronger signal than a D-Bus
  hiccup.
- **It gates this server, not the agent.** Nearly every real deployment gives
  the same agent a shell, and a shell can kill the server process or disable
  the extension. The halt switch is a stop button for a cooperating agent and
  a human's fastest reflex against a mistake. It is not containment of a
  hostile one; nothing at this layer is.
- **Injected input can, in principle, press the combination.** The compositor
  cannot tell uinput events from a keyboard. This server refuses to inject
  that combination, and the extension debounces a repeat within two seconds —
  but an agent with another injection path, or one that waits, is not stopped
  by the switch itself.
- **On a headless session, the physical key does nothing.** `WCU_SESSION=headless`
  runs a second gnome-shell with its own extension instance and its own halt
  flag, on a virtual monitor with no keyboard. A human's `Super+Ctrl+Escape`
  is delivered to the *primary* shell, which the headless server never
  consults. To halt a headless run today: stop the client, or
  `wcu-headless stop --name <name>`. This is the most conspicuous gap in the
  design and it is tracked on the ROADMAP.
- The halt gates *this server's* tools. It does not freeze the desktop, and
  it does not stop other automation stacks on the machine.

## The injection tripwire, and its honest scope

Captured screen text can contain instructions aimed at the agent ("ignore
previous instructions", "run this command") — a hijacked agent is the fastest
way to lose the trust autonomy runs on. The project's answer (ROADMAP #42) is
a **tripwire, not a filter**: OCR-captured and tree-read text is scanned for
imperative-to-agent patterns and a warning block is attached to the result.
It warns; it never blocks or redacts.

Its scope is honestly narrow: it sees only what OCR reads or the tree
exposes, it matches patterns and will miss a novel or obfuscated injection,
and a warning only helps if the model heeds it. It is a seatbelt reminder,
not a seatbelt. The real defense is the reviewing model and the evidence
trail — treat all captured screen content as untrusted data.

## The extension's D-Bus service is not access-controlled

The bundled extension owns `org.wcu.Helpers` on the **session bus** and does
not check the caller. The session bus default-allows any process running as
the same user, so once the extension is enabled, **every process on your
session bus** can call it — including `Screenshot` (full desktop, to any path,
with no portal dialog and no screen-share indicator), `GetClipboardText`,
`ClearHalt`, and the window verbs. That is true whether or not the MCP server
is running.

This matters more than "same uid, so who cares", and we would rather say it
than have you find it: on Wayland an ordinary client *cannot* screenshot, and
`xdg-desktop-portal` gates capture behind per-app consent. Installing this
extension removes that guarantee for everything on the bus. A malicious
package postinstall, a `curl | bash` script, or a Flatpak granted
`--socket=session-bus` inherits it.

Tightening this — a shared secret written 0600 at enable time and checked
against `invocation.get_sender()` — is on the ROADMAP. Until then, treat
enabling the extension as a machine-level decision, not a per-application one.
Disabling the extension (`gnome-extensions disable wcu@wayland-computer-use`)
removes the surface entirely.

## What is written to disk, and where

- **The action journal** — `$XDG_STATE_HOME/wayland-computer-use/journal/`,
  files 0600, kept 14 days. One line per *acted* tool call: arguments,
  outcome, hit/miss verdict, and the sha256 of any screenshot. Text that a
  password could be is **not** stored: `type_text`, `ui_set_text`,
  `clipboard_write` and the same three inside `do_steps` are recorded as a
  character count and a truncated digest, which is enough to tell that
  something was typed, how much, and whether it was the same string twice.
  `WCU_JOURNAL_TEXT=1` stores the characters verbatim if you want that for
  your own debugging.
  Two honest limits: **reading** tools are not journaled, so a run that only
  looks — screenshots, `clipboard_read`, `ui_read_text` — leaves no trail;
  and the journal is plain files the agent can rewrite or delete, so it is
  evidence, not an audit log. A signed append-only mode is on the ROADMAP.
- **The screenshot cache** — `~/.cache/wayland-computer-use/shots/`, directory
  0700, the last 40 captures. These are unredacted pictures of your desktop.
- **The portal restore token** —
  `$XDG_STATE_HOME/wayland-computer-use/portal-tokens.json`, 0600 in a 0700
  directory. With `persist_mode: 2` this token replays pointer injection,
  keyboard injection and monitor capture **with no consent dialog**, and it
  survives a reboot. To revoke: delete the file *and* remove the entry in
  GNOME Settings → Privacy → Screen Sharing. Only the portal backend creates
  it; the mutter backend does not.

## `launch_app` runs arbitrary programs, by design

`launch_app` takes an argv, so it is an arbitrary-code-execution primitive —
the same way a terminal is. It is what makes the tool useful, it is refused
while a halt is engaged like every other acting tool, and it is journaled.
There is no sandbox here and none is claimed. If you need one, run the whole
thing on a headless session in a VM.

## Lock-screen powers, and the config that controls them

The extension's `metadata.json` lists `"session-modes": ["user",
"unlock-dialog"]`, so it stays loaded while the screen is locked. That is a
deliberate unattended-operation feature — a long run does not die because the
lock timer fired — and it has a plainly stated cost: **a connected client can
screenshot the desktop, query windows, and write into application widgets via
AT-SPI while the screen is locked.** The lock screen protects against a
person at the keyboard, not against this server.

If that trade is wrong for your deployment, remove `"unlock-dialog"` from
`session-modes` and re-log-in: extension-backed tools then die at lock
(reported honestly by `desktop_health`), and so do long unattended runs.
AT-SPI reads/writes are a compositor-independent path and survive the lock
either way; disabling those means not running the server.

## Visibility

GNOME's orange screen-share indicator is shown whenever the pointer session
is open — that is the platform's price for the input API and this project
documents it as a feature, not a bug to hide. The extension additionally
draws an "agent is driving" border. There are no features for suppressing
either; detection evasion is explicitly out of scope (see README, Scope).

## Reporting a vulnerability

Report privately via GitHub's security advisories ("Report a vulnerability")
on this repository rather than a public issue, especially for anything in the
guard, halt, or lock-screen paths. Include the GNOME Shell version and
whether the extension was loaded (`desktop_health` output is the ideal
attachment). There is no bounty program; there is a maintainer who takes
this list seriously.
