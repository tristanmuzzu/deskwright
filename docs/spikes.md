# Moat spikes — findings (2026-08-23)

> **Update 2026-08-24:** Spike 1 is productized — `wcu-headless` +
> `WCU_SESSION=headless` (wcu/headless.py, README § "The headless second
> session"). The open input question resolved as predicted: RemoteDesktop
> follows the bus. Two things the spike missed, found in productizing:
> the session needs a private `XDG_RUNTIME_DIR` (shared at-spi socket path
> broke the PRIMARY session's a11y), and `gio launch`'s D-Bus activation
> loses the window on the private session (direct Exec spawn instead).
> Spike 2 (portal/libei) remains future work.

Two timeboxed experiments from the roadmap's Tier 3/5. Each is a
yes/no/blocked-at-layer-X finding, not shipped code.

## Spike 1 — headless second session: **YES, proven**

**Question:** can an agent drive a second GNOME session on a virtual
display while the user keeps their screen — Codex's background-parallel
feature, which nothing on Linux has?

**Result: yes, end to end, on this machine today.**

`gnome-shell --headless --wayland-display=wayland-wcu --virtual-monitor
1280x720`, started inside its own `dbus-run-session`, brings up a
complete second GNOME session on a virtual monitor. Proven in one run:

- The headless shell **loaded our own extension** (`org.wcu.Helpers` on
  the private bus) with no extra work — the extension is user-scoped, so
  the second shell scans and enables it exactly like the primary.
- `WAYLAND_DISPLAY=wayland-wcu gnome-text-editor` launched onto the
  virtual monitor; `ListWindows` over the private bus returned it.
- `Screenshot` over the private bus produced a real 1280x720 PNG of the
  virtual monitor — a full desktop (dock, top bar, the editor) that the
  user never saw on their physical screen.

**Cost:** the headless `gnome-shell` process is ~293 MB RSS. On this
8 GB machine that is real but affordable for a bounded background task;
it is not something to leave running idle.

**What makes it work:** the whole server already talks to *a* session
bus and *an* extension by name. Point `DBUS_SESSION_BUS_ADDRESS` and
`WAYLAND_DISPLAY` at the headless session and the identical code drives
it — AT-SPI, extension D-Bus, and (next) portal input all follow the
bus. No code in `wcu/` assumes the primary session.

**The one open question for productizing:** input. Screenshots and
window control go through the extension on the private bus and are
proven. Pointer/keyboard via `org.gnome.Mutter.RemoteDesktop` needs to
bind to the headless mutter — untested in this spike (the timebox went
to proving capture + window control + extension load). The portal/libei
path (Spike 2) is bus-following by design and is the likely input route
for headless. **Verdict: the hard part — a second real session that our
stack can see into — is done. Input wiring is a follow-up, not a
risk.**

**Shipping shape (proposed):** a `wcu-headless` helper that starts the
private session, a `session=headless` field on the server (or a second
server instance pinned to the private bus), and a documented "watch it"
path via `grdctl`/RDP for the user who wants to peek. Not built here.

## Spike 2 — portal + libei backend: **YES, proven (input needs one more wire)**

**Question:** does the cross-compositor route — `xdg-desktop-portal`
`RemoteDesktop` for input + `ScreenCast` for capture — work on this box,
and does a `restore_token` let a second session skip the consent dialog?
This is the path that makes KDE and wlroots possible; GNOME is just the
first place to prove it.

**Present and loadable:** `org.freedesktop.portal.RemoteDesktop` (with
`NotifyPointerMotionAbsolute`, `NotifyPointerButton`,
`NotifyKeyboardKeysym`, `ConnectToEIS`), `org.freedesktop.portal.ScreenCast`,
`libei.so.1` / `libeis.so.1` (1.5.0). GNOME's portal backend is running.

**Proven, end to end:**
- `CreateSession` → `SelectDevices(types=keyboard|pointer,
  persist_mode=2)` → `Start` brought up a working RemoteDesktop session.
- The consent dialog on first `Start` was driven **by our own stack** —
  `ui_find`/`ui_press` and `find_text`/`pointer_click` located and
  clicked "Allow Remote Interaction" and "Share". (Worth noting: the
  server can approve its own portal prompt, which is convenient for a
  headless bring-up and a thing to gate carefully in the shipped
  backend.)
- A **`restore_token` came back and was saved**, and the **second run
  reused it and reached "session started" with no dialog at all** — the
  persistence that turns a one-time consent into unattended operation.

**The one open wire:** `NotifyPointerMotionAbsolute` returned
`Invalid position`. Absolute coordinates over the portal are defined
relative to a **ScreenCast stream node** — the portal wants the input
session linked to a screen-cast stream so it knows which monitor's
coordinate space "(400, 400)" means. The spike passed a placeholder
stream id. Fixing it is mechanical: open a ScreenCast session alongside,
pass its node id. Relative motion and keyboard need no stream and were
not blocked. **Verdict: the portable route is real and persists; wiring
absolute motion to a ScreenCast node is the remaining task, not a
blocker.**

**Shipping shape (proposed):** a `portal` backend beside the extension
backend, selected when `org.wcu.Helpers` is absent (KDE, wlroots) or by
config. It opens a RemoteDesktop+ScreenCast pair, persists the
`restore_token` under `~/.local/state/wayland-computer-use/`, and maps
our absolute-coordinate API onto the stream. The extension stays the
zero-dialog fast path on GNOME. Not built here.

## Both verdicts

The two things that would make this "the Linux computer-use server" are
both **reachable on proven foundations**, not research bets:

- **Headless parallel session:** done to screenshot + window control +
  extension-load; input wiring follows the same bus.
- **Portable portal backend:** done to session + consent + token
  persistence; absolute input needs a ScreenCast-node link.

Neither blocks the current GNOME product. Both are the headline items
for the next build.

