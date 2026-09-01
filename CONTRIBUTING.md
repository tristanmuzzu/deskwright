# Contributing

Small project, strong opinions. The short version: measurements over
assumptions, evidence over assertion, and never delete a comment that carries
a measured finding.

## Module map

The MCP server lives in `deskwright/`; `mcp_server.py` is the thin entry point.

| Module | What it owns |
|---|---|
| `deskwright/server.py` | The `TOOLS` registry, JSON-RPC dispatch, `--self-test`, and the halt gate (state-changing tools refuse while the human halt switch is engaged; reading tools keep working). |
| `deskwright/shell.py` | Everything answered by the bundled GNOME Shell extension over D-Bus: window list/geometry/focus, `window_manage`, `wait_for`, `assert_state`, the halt probe. |
| `deskwright/atspi.py` | The accessibility tree: `ui_apps` / `ui_tree` / `ui_find` / `ui_press` / `ui_read_text` / `ui_set_text`, `launch_app`, widget-identity re-checks. |
| `deskwright/input.py` | Pointer and keyboard injection (Mutter RemoteDesktop keysyms first, ydotool fallback), `screen_map`, clipboard, `hold_key`, drag verification, the refused-combination guards. |
| `deskwright/capture.py` | `screenshot`, `zoom`, `screencast`, `frames`, `region_changed`, and the before/after "look" machinery every acting tool reports through. |
| `deskwright/ocr.py` | `find_text`, OCR to screen coordinates. |
| `deskwright/steps.py` | `do_steps`: up-front validation of the whole sequence, per-step retry, the one-picture-at-the-end contract. |
| `deskwright/errors.py` | `ToolError` and the stable machine-readable error codes agents branch on. |
| `deskwright/config.py` | Paths and constants. |
| `deskwright/extension/` | The compositor-side half: `com.zeticle.deskwright` D-Bus service inside gnome-shell (screenshots, windows, pointer position, halt keybinding, indicator). Changes here need a logout/login to load, see deskwright/extension/README.md. |

`deskwright/atspi_ui.py`, `deskwright/desktop.py`, `deskwright/remote_input.py`,
`deskwright/portal_input.py`, `deskwright/screencast.py` and `deskwright/frames.py` are the
standalone CLI forerunners. `deskwright-atspi` and `deskwright-desktop` are installed as
console scripts; the other two run as `python3 -m deskwright.<name>` from a
checkout. They are
not vestigial: `deskwright/remote_input.py` and `deskwright/portal_input.py` are the two
input backends, and `deskwright/capture.py` shells out to `deskwright/screencast.py` and
`deskwright/frames.py` because a recording has to outlive one tool call.

`deskwright/cli.py` is the `deskwright` console script and
`mcp_server.py` at the root is the same entry point by the path existing
MCP registrations already point at; both go through `deskwright/session.py` for
headless pinning. Everything the server needs at runtime lives inside the
`deskwright` package, extension included, so the wheel is a complete install.

## Tests, and which need a live desktop

Every test file states its own requirements in its docstring; the split today:

**Headless: run anywhere, including CI and a locked machine.**

- `tests/test_steps_validation.py`: `do_steps` refuses before executing;
  pure in-process, handlers stubbed.
- `tests/test_zoom_and_delta.py`: `zoom` and the "what changed" summary;
  no desktop.
- `tests/test_atspi_addressing.py`: `launch_app` and document addressing;
  in-process.

**Need a live GNOME Wayland session (and mostly the loaded extension):**

- `tests/test_look.py`: images really come back inline, hit != miss.
- `tests/test_pointer.py`: puts a witness window on screen and asserts
  against what it actually received.
- `tests/test_e2e_real_task.py`: a real task through the real protocol.
- `tests/test_screencast.py`: a recording is a real recording.
- `tests/test_clipboard.py`: real wl-copy/wl-paste and the real extension;
  it saves and restores the prior clipboard text and injects no input.

The desktop is a **serialized test resource**: any number of branches can be
written in parallel, but only one thing at a time may drive the real screen.
Run the live suites one at a time, from one session.

## The mcpdrv trick

The server an MCP client (Claude Code included) holds open is the code that
was on disk **when the session started**. Edit the server and nothing you
observe through the client changes, which looks exactly like your change not
working. `tests/mcpdrv.py` exists for this: it launches a fresh server
subprocess and speaks MCP to it over stdio, so an edit is measurable in the
same minute it is written, and an A/B against `git stash` is possible at all.

```bash
./tests/mcpdrv.py tools
./tests/mcpdrv.py screenshot '{"path":"/tmp/a.png","inline":true}'
```

Never conclude a change is broken from the client-held server alone.

## Comment philosophy

Comments in this codebase carry **measured findings**, dates, numbers, the
denial message a compositor actually returned, the incident a guard exists
because of. They are the difference between "this looks removable" and "this
was learned the hard way on 2026-08-16". Do not delete them when refactoring;
move them with the code they explain. If you re-measure and get a different
number, update the comment with the new number and date rather than removing
the old claim.

## Commit style

Observed style, keep it: `feat:` / `fix:` / `docs:` / `test:` / `refactor:`
prefixes, lowercase, and a subject that states the *finding or behavior*, not
the diff, `fix: German keyboard layout silently corrupts every ydotool
keystroke`, not `fix: keyboard bug`. One logical change per commit; every
commit leaves the machine working (`./mcp_server.py --self-test` plus the
relevant suites pass). Branch per change, merge serially, live-verify per
merge.

## Before you open a PR

1. `./mcp_server.py --self-test` on a real session, plus the headless suites.
2. If you touched the extension: syntax-check
   (`deskwright/extension/deskwright@zeticle.com/check-syntax.sh`), and say in the PR
   that the change needs a re-login to verify, reviewers cannot see it live
   until then.
3. New behavior gets a test in the right category above, headless if it can
   possibly be headless.
