# Claude Code setup

The configuration this server is designed for: registered at user scope, every
tool pre-approved. What follows is the intended setup, not the permissive end of
a spectrum.

The reasoning is the same table the README opens with. A tool call that has to
stop and wait for a human is not a slower tool call, it is a different mode of
operation — the run ends there until someone comes back to it, and "drive my
desktop while I am not at it" is the entire point. A prompt in front of
`pointer_click` also buys very little that is not already bought: the click is
refused if `expect_window` says the compositor would deliver it elsewhere,
`ui_press` is refused if the widget under the path no longer matches
`expect_name`/`expect_role`, every injecting tool proves focus landed before it
types anything, and `Ctrl+Alt+F1`…`F12` is refused outright. The guards are in
the server, where they can check something. A dialog can only ask a human who is
not looking at the screen.

## Registering the server

User scope, so the tools are present in every session on the machine rather than
only in this repository:

```bash
cd /path/to/wayland-computer-use
claude mcp add wayland-computer-use --scope user -- "$PWD/mcp_server.py"
```

The `--` matters: everything after it is the command that runs the server, and
without it `claude mcp add` reads the server's own flags as its own. The path
must be absolute — user scope means the entry is used from every directory, and
a relative `./mcp_server.py` would resolve against whatever project the session
happened to start in. `mcp_server.py` has a shebang and the executable bit; if
that has been lost, use `-- python3 "$PWD/mcp_server.py"` instead.

The equivalent as a `.mcp.json` block at a project root:

```json
{
  "mcpServers": {
    "wayland-computer-use": {
      "command": "/home/you/projects/wayland-computer-use/mcp_server.py",
      "args": []
    }
  }
}
```

`.mcp.json` is the project-scope form and is checked into a repository to share
with a team. It costs an approval step that user scope does not: a
project-scoped server sits at `⏸ Pending approval` until the folder is trusted,
and `permissions.allow` rules from a project's `.claude/settings.json` are
likewise only applied after the workspace trust dialog is accepted. Allow rules
in your own `~/.claude/settings.json` are not subject to that. For a desktop
automation server used from everywhere, user scope is the smaller configuration.

Confirm it connected before writing permissions for it:

```bash
claude mcp list                        # health per server
claude mcp get wayland-computer-use    # the entry as Claude Code resolved it
```

## The allowlist

Claude Code names an MCP tool `mcp__<server-name>__<tool-name>`. The server-name
segment is **the name you registered the server under** — the first argument to
`claude mcp add`, or the key under `mcpServers` — not the name the server
advertises about itself in its handshake. Those happen to agree here
(`SERVER_INFO` in `mcp_server.py` says `wayland-computer-use`), which makes the
distinction easy to miss until someone registers it as `desktop` and every rule
below silently matches nothing. Register it under `wayland-computer-use` or
substitute your name everywhere in this section.

Allow rules accept a wildcard in the tool position after a literal
`mcp__<server>__` prefix, so the whole configuration is one line in
`~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__wayland-computer-use__*"
    ]
  }
}
```

A bare `mcp__wayland-computer-use`, with no tool segment, matches every tool of
the server too. Two limits on the syntax are worth knowing: the server segment
itself must be glob-free, and an unanchored allow glob — `"*"`, `"mcp__*"` — is
skipped with a warning and approves nothing, because those forms are only
meaningful in `deny` and `ask`. There is no way to write "allow every MCP
server" as an allow rule, only "allow this one".

Allow rules work in the default permission mode. Nothing here needs
`bypassPermissions`.

### Enumerated, if you would rather see the list

Identical in effect to the wildcard. The argument for it is that a tool added by
a future version is not silently pre-approved, and that the diff is loud when
one is:

```json
{
  "permissions": {
    "allow": [
      "mcp__wayland-computer-use__activate_window",
      "mcp__wayland-computer-use__desktop_health",
      "mcp__wayland-computer-use__do_steps",
      "mcp__wayland-computer-use__find_text",
      "mcp__wayland-computer-use__frames",
      "mcp__wayland-computer-use__list_windows",
      "mcp__wayland-computer-use__pointer_click",
      "mcp__wayland-computer-use__pointer_drag",
      "mcp__wayland-computer-use__pointer_move",
      "mcp__wayland-computer-use__pointer_position",
      "mcp__wayland-computer-use__pointer_scroll",
      "mcp__wayland-computer-use__press_keys",
      "mcp__wayland-computer-use__region_changed",
      "mcp__wayland-computer-use__screen_map",
      "mcp__wayland-computer-use__screencast",
      "mcp__wayland-computer-use__screenshot",
      "mcp__wayland-computer-use__type_text",
      "mcp__wayland-computer-use__ui_apps",
      "mcp__wayland-computer-use__ui_find",
      "mcp__wayland-computer-use__ui_press",
      "mcp__wayland-computer-use__ui_read_text",
      "mcp__wayland-computer-use__ui_set_text",
      "mcp__wayland-computer-use__ui_tree",
      "mcp__wayland-computer-use__wait_for",
      "mcp__wayland-computer-use__window_at"
    ]
  }
}
```

That is all 25 tools the server serves. `tools/list` is the authority; check it
against a running server with `./tests/mcpdrv.py tools`.

## The cautious variant

If you would rather approve the calls that touch the machine, the split falls
along the tools that inject: `pointer_click`, `pointer_drag`, `pointer_move`,
`pointer_scroll`, `press_keys`, `type_text`, `ui_press`, `ui_set_text` and
`do_steps`, which is on the list because it runs a sequence of the others in one
call. Leave the allowlist above in place and add those nine to `permissions.ask`
— `ask` is evaluated ahead of `allow`, from any scope, so nothing needs removing
from the allow list to make it take effect. Everything that only looks
(`screenshot`, `screen_map`, `find_text`, `ui_find`, `ui_read_text`, `ui_tree`,
`ui_apps`, `list_windows`, `window_at`, `pointer_position`, `wait_for`,
`region_changed`, `screencast`, `frames`, `desktop_health`, `activate_window`)
stays approved, which keeps the expensive half of a session — the looking —
unprompted. The cost is concentrated in `do_steps`: batching a known sequence
into one call is the main thing that makes a long task cheap, and a prompt in
front of it gives that back.

## Two operational notes

Claude Code reloads settings files while it runs, so an edit to `permissions`
applies to the session in progress. The server is not like that: the process
Claude Code is holding open is whatever `mcp_server.py` was on disk when the
session started, so a change to the server is invisible until a restart — or
until you speak MCP to a fresh copy with `tests/mcpdrv.py`.

And the requirements in the README are not optional for a working setup. In
particular `toolkit-accessibility` must be true *before* an application starts,
or that application's tree is stunted in a way that reads as "this app has no
widgets".
