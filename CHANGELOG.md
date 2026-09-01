# Changelog

## 0.1.1

Adds the `mcp-name: io.github.tristanmuzzu/deskwright` line to the README. The MCP registry reads the README that
ships with the PyPI package and looks for exactly that string to prove the
package and the registry entry have the same owner. PyPI metadata is immutable
per release, so the marker could only reach it in a new version. No code
changed.

## 0.1.0, first public release

Named **Deskwright**. It was `wayland-computer-use` during development, until
that name turned out to be taken on PyPI by an unrelated project. Renaming
before anything shipped was cheaper than living beside a lookalike, and the
new name survives compositors that are not GNOME.

If you installed a pre-release under the old name, two things keep working
until you remove them, and both go in 0.2.0: `WCU_*` environment variables are
read when their `DESKWRIGHT_*` counterpart is unset, and the old
`org.wcu.Helpers` extension is still accepted on the bus, because gnome-shell
keeps running it until your next logout.

First release to PyPI, the MCP registry and the Claude Code plugin directory.

**Install** is `pipx install --system-site-packages deskwright` then
`deskwright-setup`, with no checkout: the gnome-shell extension ships inside the
wheel and `deskwright-setup` copies it into place. `--system-site-packages` is
required because PyGObject publishes no wheels to PyPI.

**33 tools** over MCP, ordered accessibility-tree-first:

- `ui_apps` / `ui_tree` / `ui_find` / `ui_press` / `ui_read_text` /
  `ui_set_text`: the accessibility tree, with widget-identity re-checks.
- `pointer_click` / `_move` / `_drag` / `_scroll` / `pointer_position` /
  `screen_map` / `window_at`, compositor-performed pointer input in absolute
  screen coordinates, with `expect_window` refusing a click that would land
  elsewhere.
- `type_text` / `press_keys` / `hold_key`, compositor keysyms, focus proven
  first, `ydotool` only as a fallback.
- `screenshot` / `zoom` / `region_changed` / `find_text` (OCR) /
  `screencast` / `frames`, looking, including motion.
- `launch_app` / `list_windows` / `activate_window` / `window_manage` /
  `clipboard_read` / `clipboard_write`: the desktop itself.
- `do_steps` / `wait_for` / `assert_state` / `journal` / `desktop_health`,
  batching, waiting, proving and reviewing.

**The headless second session.** `DESKWRIGHT_SESSION=headless` (or `headless:<name>`)
runs everything on a private virtual monitor the user never sees. Named
sessions are isolated from each other.

**Two input backends.** `org.gnome.Mutter.RemoteDesktop` when it answers (no
consent dialog), `org.freedesktop.portal.RemoteDesktop` otherwise (one
consent, persisted via `restore_token`). `DESKWRIGHT_INPUT_BACKEND` forces either.

**Evidence and a halt switch.** Every acted call is journaled;
`Super+Ctrl+Escape` is grabbed inside the compositor and this server refuses
to inject it. See [SECURITY.md](SECURITY.md) for exactly what each promises
and what it does not.

Verified on GNOME Shell 50.1 (Ubuntu 26.04). 48–49 declared, untested.
