# This machine

Traps measured on Tristan's laptop (Ubuntu 26.04, GNOME Shell 50). Keep only what cost a real run.

## Private desktops

- Both servers send `balanced` screenshots (`DESKWRIGHT_IMAGE_PROFILE`). The private server is launched by `skills/deskwright-codex/scripts/private_server.py --prefix claude`. Each MCP connection, which means each Claude session, gets a desktop named `claude-<hash>` and a private app home under `~/.local/share/deskwright/claude-desktops/`. An empty desktop is stopped when the session ends; one with open windows is kept for recovery.
- The first tool call starts the desktop: 15 to 20 seconds cold. Each desktop costs about 205 MB. `deskwright-headless list` shows them and free memory; a start that would push the machine into swap is refused. `deskwright-headless stop --name NAME` stops one of yours. Never stop another task's desktop.
- Only the screen and app home are private. Files you open by absolute path, and dconf settings, are Tristan's real ones. An edit there is a real edit.
- Stateful apps restore their own state. On 2026-08-27 a text editor on a shared home reopened one of Tristan's unsaved drafts and a test typed into it. Act on the window IDs `launch_app` returns, never on window counts or "the" window, and check a scratch app opens empty.

## Input and capture

- `launch_app` with a `desktop_id` spawns the Exec line directly, because D-Bus activation loses the window on a private desktop. Do not bypass `launch_app`.
- `screencast` records only while pixels change, so a still desktop yields a 0-second file. Record during the action; use `screenshot` for still evidence.
- There is no XWayland on private desktops; X11-only helpers do not run there.
- ydotool is refused there (`wrong_session`) because it would type into Tristan's real seat. Never work around this. On the primary desktop, never inject `Ctrl+Alt+F1` to `F12`: that switches the session and looks like a frozen machine.
- A shell-level modal (keyring or auth prompt) is invisible to `list_windows` and makes every click report "click-through". Screenshot first, then click the dialog's own buttons by coordinate. Launch Chrome there with `--password-store=basic` on a throwaway profile to avoid the prompt.

## Verification

`DESKWRIGHT_SESSION=headless deskwright --self-test`, and `tests/test_e2e_real_task.py` in the repo.
