# Setting this up, for an agent

This file is a runbook. If someone handed you this repo and asked you to set it
up, follow it top to bottom. Every command is here, along with how to check it
worked and the handful of things that go wrong.

You are installing an MCP server that drives a GNOME Wayland desktop, plus a
gnome-shell extension that has to be loaded by the compositor. The extension is
the reason this isn't a one-liner: gnome-shell only picks up an extension at
session start, so there's exactly one logout in the middle that nobody can
automate away.

Tell the user that up front. Two commands, one logout, one line to register.

## 0. Check you're on a machine this works on

```bash
echo "$XDG_CURRENT_DESKTOP / $XDG_SESSION_TYPE"
```

You want something containing `GNOME` and the word `wayland`. Anything else and
you should stop and say so. This drives GNOME Shell through its own extension
and through `org.gnome.Mutter.RemoteDesktop`. On KDE or Sway the input half
works through xdg-desktop-portal but window management and screenshots don't,
so `desktop_health` will report the desktop as not usable and it will be
telling the truth.

## 1. Install the package

```bash
pipx install --system-site-packages deskwright
```

If `pipx` isn't there: `sudo apt install pipx` on Debian and Ubuntu,
`sudo dnf install pipx` on Fedora, `sudo pacman -S python-pipx` on Arch.

`--system-site-packages` is not optional and not a style choice. PyGObject
publishes no wheels to PyPI, so it's a distro package on every distro, and an
isolated venv can't import it. Leave the flag off and you get a server that
starts fine and then fails on the first click. `uvx` has no equivalent flag,
which is why this says pipx.

If `pipx` warns that its bin directory isn't on PATH, run `pipx ensurepath` and
start a new shell.

## 2. Find out what the machine is missing

```bash
deskwright-setup --check
```

Read-only. It changes nothing, and it exits nonzero if a hard requirement is
absent. For every missing thing it prints the exact install line for the distro
it detected, so you don't have to guess the package name.

Hard requirements, in case you want to install them in one go first:

| | Debian / Ubuntu | Fedora | Arch |
|---|---|---|---|
| PyGObject | `python3-gi` | `python3-gobject` | `python-gobject` |
| AT-SPI typelib | `gir1.2-atspi-2.0` | `at-spi2-core` | `at-spi2-core` |
| Pillow | `python3-pil` | `python3-pillow` | `python-pillow` |
| gdbus | `libglib2.0-bin` | `glib2` | `glib2` |
| clipboard | `wl-clipboard` | `wl-clipboard` | `wl-clipboard` |
| OCR | `tesseract-ocr` | `tesseract` | `tesseract` |

Those need root. If you can't run `sudo`, print the line and ask the user to
run it. Don't try to work around it.

The AT-SPI typelib is the one people miss. `python3-gi` on its own is not
enough, and without the typelib every `ui_*` tool fails with "Namespace Atspi
not available", which names no package and sends people in circles.

## 3. Apply the setup

```bash
deskwright-setup
```

This never runs `sudo`. It:

1. turns on `org.gnome.desktop.interface toolkit-accessibility`, which is what
   makes applications expose an accessibility tree at all
2. copies the bundled gnome-shell extension into
   `~/.local/share/gnome-shell/extensions/` and enables it
3. prints the systemd unit for the optional `ydotoold` fallback, without
   running it
4. prints the line that registers the server with your MCP client

It's idempotent. Run it twice and the second run says everything is already in
place. Every change prints its before and after value.

## 4. The logout

**Tell the user to log out and log back in.** There is no way around this and
no point trying. On Wayland gnome-shell cannot reload an extension in place:
`ReloadExtension` answers "deprecated and does not work" on GNOME 50, and
disable/enable re-runs `enable()` against a module the shell has already
imported. The copied code is not running until the next login.

One thing worth telling them: the accessibility flag from step 3 is read by
applications at startup, so anything already open keeps a stunted tree until it
restarts. The logout fixes that too.

Until the logout the server still works through its fallbacks, so if the user
wants to poke at it first, that's fine. AT-SPI, pointer and keyboard input, and
the clipboard are all live. What's missing is window management, extension
screenshots, pointer position and the halt switch.

## 5. Register it with the client

For Claude Code:

```bash
claude mcp add deskwright --scope user -- deskwright
```

(If the user installed the Claude Code plugin instead of the pip package, skip
this: the plugin registers the server itself. Steps 2 to 4 still apply, and
`deskwright-setup` lives at
`~/.claude/plugins/marketplaces/deskwright/bin/deskwright-setup`.)

User scope, so the tools are there in every project rather than one. For any
other MCP client, the command is `deskwright` with no arguments and
it speaks MCP on stdio.

If you want unattended runs to work without a permission prompt on every call,
`docs/claude-code-setup.md` has the allowlist and the reasoning behind it.

## 6. Prove it works

```bash
DESKWRIGHT_SESSION=headless deskwright --self-test
```

This runs on a private virtual monitor rather than the user's screen, so it's
safe to run while they're working. It costs about 20 seconds the first time
because it has to start a second gnome-shell.

You want `18/18 passed`. Anything less and the report names which capability
failed.

Drop `DESKWRIGHT_SESSION=headless` to test the real desktop instead, but only when the
user is looking at the screen: the self-test injects real input, because it
probes the key-combination guards.

## When it doesn't work

**`desktop_health` says the extension is INACTIVE.** They didn't log out, or
they logged out before step 3 finished. Ask.

**Every `ui_*` call says "Namespace Atspi not available".** The typelib from
step 2 is missing. `deskwright-setup --check` will confirm.

**Pointer or typing fails with `input_backend_failed`.** Almost always pipx
without `--system-site-packages`. Check with:

```bash
"$(pipx environment --value PIPX_LOCAL_VENVS)/deskwright/bin/python" -c "import gi; print('ok')"
```

If that fails, reinstall with the flag:
`pipx install --force --system-site-packages deskwright`.

**`deskwright-setup` refuses with "this machine is not a target".** It read
`XDG_CURRENT_DESKTOP` and didn't find GNOME. That's step 0. If you're
deliberately preparing a machine you're not logged into yet, `deskwright-setup --force`
skips the check.

**Apps show no widgets in `ui_tree`.** They were running before
`toolkit-accessibility` got turned on. Restart the app, or log out.

**A tool returns `halted`.** A human pressed `Super+Ctrl+Escape`. That's the
halt switch. Stop, and tell them you noticed. Press it again to clear it, or
call `ClearHalt` on the extension.

## What to tell the user when you're done

Something like this:

> Installed and registered. Log out and back in once, then ask me to open an
> app and I'll drive it. If you'd rather I work on a desktop you can't see so I
> don't steal your focus, say so and I'll use `DESKWRIGHT_SESSION=headless`.

## Working on the code instead of installing it

If the task is development rather than setup, read `CONTRIBUTING.md`. Short
version: the fast suite is `python3 -m pytest -q` and runs anywhere,
`ruff check .` has to be clean, and the suites that prove anything interesting
need a real session and are listed in `CONTRIBUTING.md` with what each one
needs. Never edit the extension and assume it's live. It isn't until the next
login.
