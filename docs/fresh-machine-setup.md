# Fresh-machine setup

Two commands, no checkout:

```sh
pipx install --system-site-packages deskwright
deskwright-setup                  # apply (idempotent, never sudos)
deskwright-setup --check          # read-only report, nonzero exit if something hard is missing
```

The gnome-shell extension travels inside the wheel (`deskwright/extension/`), so
there is nothing to clone: `deskwright-setup` copies it out of the installed package.

`--system-site-packages` is load-bearing. PyGObject publishes no wheels to
PyPI (verified 2026-08-30 -- `uv pip install PyGObject --no-build` answers
"all versions of pygobject have no usable wheels"), so it is a distro package
everywhere and an isolated environment cannot import it. `uvx` has no
equivalent flag, which is why `pipx` is the documented route.

From a clone, the same thing without installing anything:

```sh
git clone https://github.com/tristanmuzzu/deskwright
cd deskwright
./bin/deskwright-setup
```

## What it does, in order

1. **Dependencies**, detects each and prints the exact install line for your
   distro family (apt/dnf/pacman, from `/etc/os-release`; apt phrasing when
   unrecognized). It never runs the install itself. Hard requirements:
   `python3-gi` (system package, PyGObject is not sanely pip-installable),
   Pillow, GLib's `gdbus`, `wl-clipboard`, `tesseract`. Optional: `ydotool`
   (fallback input path only).
2. **`gsettings set org.gnome.desktop.interface toolkit-accessibility true`**,
   idempotent, before/after printed. Applications read this at startup:
   anything already running keeps a stunted accessibility tree until that app
   restarts.
3. **The bundled gnome-shell extension**, copies
   `deskwright/extension/deskwright@zeticle.com` to
   `~/.local/share/gnome-shell/extensions/` and enables it. A freshly copied
   extension is invisible to `gnome-extensions enable` ("does not exist")
   until the shell rescans at login, so enabling goes through the
   `org.gnome.shell enabled-extensions` gsettings list, which works
   immediately. Then a **log out / log in is required**, on Wayland
   gnome-shell cannot reload an extension in place. Until then the server
   already works through its fallbacks (AT-SPI, RemoteDesktop input,
   wl-clipboard); after login you additionally get the window verbs,
   extension screenshots, pointer position, and the human halt key.
4. **ydotoold** (optional), prints the systemd unit and commands. They need
   root, so they are printed, never run.
5. **Claude Code registration**, prints the `claude mcp add` line. Details
   and the auto-approval allowlist: [claude-code-setup.md](claude-code-setup.md).

Finally it points at `deskwright --self-test` (or
`./mcp_server.py --self-test` from a clone), which it does not run for you:
the self-test injects input, since it probes the key-combo guards. Run it
while looking at the screen -- or add `DESKWRIGHT_SESSION=headless` and run it on a
desktop you cannot see.

Safe to run repeatedly: every state change is printed with its before/after
value, and a second run finds everything already in place and says so.
