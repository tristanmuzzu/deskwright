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
  that has not loaded it (before the post-install re-login, or if the
  extension is disabled), `halt_active()` answers no at zero cost — and a
  probe failure also counts as not-halted, deliberately: the switch exists to
  let a human stop the server, never to let a D-Bus hiccup stop it. If you
  need a guaranteed stop without the extension, kill the server process or
  the client holding it.
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
