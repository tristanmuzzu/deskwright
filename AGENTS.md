# Deskwright: agent guide

An MCP server that drives a GNOME Wayland desktop, plus a gnome-shell extension
loaded by the compositor. Read by operation: this file holds the development
invariants and the routes; setup itineraries live in `docs/` and apply only to
an explicit setup task.

## Development invariants

If the task is development rather than setup, read `CONTRIBUTING.md`. Short
version: the fast suite is `python3 -m pytest -q` and runs anywhere,
`ruff check .` has to be clean, and the suites that prove anything interesting
need a real session and are listed in `CONTRIBUTING.md` with what each one
needs. Never edit the extension and assume it's live. It isn't until the next
login.

Guardrails that hold in every task:

- The extension is not live until the next login. gnome-shell cannot reload it
  in place on Wayland; do not log the user out to make it live, and do not
  claim live verification before a login has happened.
- `deskwright-setup` never runs `sudo`; root-needing package installs are
  printed for the user. Do not work around a missing `sudo`.
- `pipx install --system-site-packages` is load-bearing (PyGObject has no
  wheels). Do not "clean up" the flag.
- The self-test on the real desktop injects real input (it probes the
  key-combination guards); run it only when the user is watching the screen.
  `DESKWRIGHT_SESSION=headless deskwright --self-test` uses a private virtual
  monitor and is safe while they work.
- A tool returning `halted` means a human pressed `Super+Ctrl+Escape`. Stop
  and say you noticed; do not clear it silently.
- Do not treat a similar-looking checkout or a matching `package.json` as
  proof that a directory is disposable.

## Routes

- **Setting the package up on a machine:** `docs/agent-setup-runbook.md`
  (full itinerary, conditional) or the two-command
  `docs/fresh-machine-setup.md`. Moving the runbook out of this file is not
  permission to rerun it.
- **Registering with Codex on Linux:** `docs/codex-setup.md` (the two
  `codex mcp add` lines, timeouts, private headless session, and the
  `skills/deskwright-codex/` skill). Do not edit that skill or the session
  code as part of a guidance change.
- **Registering with Claude Code:** `docs/claude-code-setup.md` (allowlist and
  reasoning); `claude mcp add deskwright --scope user -- deskwright`.
- **Execution semantics:** `docs/execution-contract.md`; measured incidents in
  `docs/field-notes.md` and `docs/spikes.md`.
- **Contributing:** `CONTRIBUTING.md` (module map, suites, what each needs).
