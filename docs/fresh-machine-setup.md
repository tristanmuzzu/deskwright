# Fresh-machine setup

One command from a clone:

```sh
git clone <this repo> && cd wayland-computer-use
./bin/wcu-setup            # apply (idempotent, never sudos)
./bin/wcu-setup --check    # read-only report, nonzero exit if something hard is missing
```

`uvx --from . wcu-setup --check` and `pipx run --spec . wcu-setup` work too
(the `wcu-setup` entry point in `pyproject.toml`); either way the checkout is
still needed, because the extension and `mcp_server.py` are used from it.

## What it does, in order

1. **Dependencies** — detects each and prints the exact install line for your
   distro family (apt/dnf/pacman, from `/etc/os-release`; apt phrasing when
   unrecognized). It never runs the install itself. Hard requirements:
   `python3-gi` (system package — PyGObject is not sanely pip-installable),
   Pillow, GLib's `gdbus`, `wl-clipboard`, `tesseract`. Optional: `ydotool`
   (fallback input path only).
2. **`gsettings set org.gnome.desktop.interface toolkit-accessibility true`** —
   idempotent, before/after printed. Applications read this at startup:
   anything already running keeps a stunted accessibility tree until that app
   restarts.
3. **The bundled gnome-shell extension** — copies
   `extension/wcu@wayland-computer-use` to
   `~/.local/share/gnome-shell/extensions/` and enables it. A freshly copied
   extension is invisible to `gnome-extensions enable` ("does not exist")
   until the shell rescans at login, so enabling goes through the
   `org.gnome.shell enabled-extensions` gsettings list, which works
   immediately. Then a **log out / log in is required** — on Wayland
   gnome-shell cannot reload an extension in place. Until then the server
   already works through its fallbacks (AT-SPI, RemoteDesktop input,
   wl-clipboard); after login you additionally get the window verbs,
   extension screenshots, pointer position, and the human halt key.
4. **ydotoold** (optional) — prints the systemd unit and commands. They need
   root, so they are printed, never run.
5. **Claude Code registration** — prints the `claude mcp add` line. Details
   and the auto-approval allowlist: [claude-code-setup.md](claude-code-setup.md).

Finally it points at `./mcp_server.py --self-test`, which it does not run for
you: the self-test injects input (it probes the key-combo guards), so run it
while looking at the screen.

Safe to run repeatedly: every state change is printed with its before/after
value, and a second run finds everything already in place and says so.
