# Claude Code setup

The configuration this server is designed for: registered at user scope, every
tool pre-approved. What follows is the intended setup, not the permissive end of
a spectrum.

The reasoning is the same table the README opens with. A tool call that has to
stop and wait for a human is not a slower tool call. It is a different mode of
operation: the run ends there until someone comes back to it, and "drive my
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
claude mcp add deskwright --scope user -- deskwright
```

`deskwright` is the console script installed by
`pipx install --system-site-packages deskwright`. From a clone
instead, name the file by its absolute path:

```bash
cd /path/to/deskwright
claude mcp add deskwright --scope user -- "$PWD/mcp_server.py"
```

The `--` matters: everything after it is the command that runs the server, and
without it `claude mcp add` reads the server's own flags as its own. The path
must be absolute, user scope means the entry is used from every directory, and
a relative `./mcp_server.py` would resolve against whatever project the session
happened to start in. `mcp_server.py` has a shebang and the executable bit; if
that has been lost, use `-- python3 "$PWD/mcp_server.py"` instead.

As a Claude Code plugin, neither step is needed. The plugin registers the
server itself:

```bash
claude plugin marketplace add tristanmuzzu/deskwright
claude plugin install deskwright@deskwright
```

The equivalent as a `.mcp.json` block at a project root:

```json
{
  "mcpServers": {
    "deskwright": {
      "command": "/home/you/projects/deskwright/mcp_server.py",
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
claude mcp get deskwright    # the entry as Claude Code resolved it
```

## The allowlist

Claude Code names an MCP tool `mcp__<server-name>__<tool-name>`. The server-name
segment is **the name you registered the server under**, the first argument to
`claude mcp add`, or the key under `mcpServers`, not the name the server
advertises about itself in its handshake. Those happen to agree here
(`SERVER_INFO` in `mcp_server.py` says `deskwright`), which makes the
distinction easy to miss until someone registers it as `desktop` and every rule
below silently matches nothing. Register it under `deskwright` or
substitute your name everywhere in this section.

Allow rules accept a wildcard in the tool position after a literal
`mcp__<server>__` prefix, so the whole configuration is one line in
`~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__deskwright__activate_window",
      "mcp__deskwright__assert_state",
      "mcp__deskwright__clipboard_read",
      "mcp__deskwright__clipboard_write",
      "mcp__deskwright__desktop_health",
      "mcp__deskwright__do_steps",
      "mcp__deskwright__find_text",
      "mcp__deskwright__frames",
      "mcp__deskwright__hold_key",
      "mcp__deskwright__journal",
      "mcp__deskwright__launch_app",
      "mcp__deskwright__list_windows",
      "mcp__deskwright__pointer_click",
      "mcp__deskwright__pointer_drag",
      "mcp__deskwright__pointer_move",
      "mcp__deskwright__pointer_position",
      "mcp__deskwright__pointer_scroll",
      "mcp__deskwright__press_keys",
      "mcp__deskwright__region_changed",
      "mcp__deskwright__screen_map",
      "mcp__deskwright__screencast",
      "mcp__deskwright__screenshot",
      "mcp__deskwright__type_text",
      "mcp__deskwright__ui_apps",
      "mcp__deskwright__ui_find",
      "mcp__deskwright__ui_press",
      "mcp__deskwright__ui_read_text",
      "mcp__deskwright__ui_set_text",
      "mcp__deskwright__ui_tree",
      "mcp__deskwright__wait_for",
      "mcp__deskwright__window_at",
      "mcp__deskwright__window_manage",
      "mcp__deskwright__zoom"
    ]
  }
}
```

A bare `mcp__deskwright`, with no tool segment, matches every tool of
the server too. Two limits on the syntax are worth knowing: the server segment
itself must be glob-free, and an unanchored allow glob, `"*"`, `"mcp__*"`, is
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
      "mcp__deskwright__activate_window",
      "mcp__deskwright__desktop_health",
      "mcp__deskwright__do_steps",
      "mcp__deskwright__find_text",
      "mcp__deskwright__frames",
      "mcp__deskwright__list_windows",
      "mcp__deskwright__pointer_click",
      "mcp__deskwright__pointer_drag",
      "mcp__deskwright__pointer_move",
      "mcp__deskwright__pointer_position",
      "mcp__deskwright__pointer_scroll",
      "mcp__deskwright__press_keys",
      "mcp__deskwright__region_changed",
      "mcp__deskwright__screen_map",
      "mcp__deskwright__screencast",
      "mcp__deskwright__screenshot",
      "mcp__deskwright__type_text",
      "mcp__deskwright__ui_apps",
      "mcp__deskwright__ui_find",
      "mcp__deskwright__ui_press",
      "mcp__deskwright__ui_read_text",
      "mcp__deskwright__ui_set_text",
      "mcp__deskwright__ui_tree",
      "mcp__deskwright__wait_for",
      "mcp__deskwright__window_at"
    ]
  }
}
```

That is all 33 tools the server serves. `tools/list` is the authority;
CI fails if this list and the server's disagree. To check a running server
yourself: `./tests/mcpdrv.py tools` from a checkout.

## The cautious variant

If you would rather approve the calls that touch the machine, the split falls
along the tools the server itself treats as acting -- the same 14 it
journals and the same 14 the halt switch gates:

- `activate_window`
- `clipboard_write`
- `do_steps`
- `hold_key`
- `launch_app`
- `pointer_click`
- `pointer_drag`
- `pointer_move`
- `pointer_scroll`
- `press_keys`
- `type_text`
- `ui_press`
- `ui_set_text`
- `window_manage`

`do_steps` is on that list because it runs a sequence of the others in one
call. Leave the allowlist above in place and add those 14 to
`permissions.ask` -- `ask` is evaluated ahead of `allow`, from any scope, so
nothing needs removing from the allow list to make it take effect.

Everything that only looks stays approved, which keeps the expensive half of a
session -- the looking -- unprompted:

- `assert_state`
- `clipboard_read`
- `desktop_health`
- `find_text`
- `frames`
- `journal`
- `list_windows`
- `pointer_position`
- `region_changed`
- `screen_map`
- `screencast`
- `screenshot`
- `ui_apps`
- `ui_find`
- `ui_read_text`
- `ui_tree`
- `wait_for`
- `window_at`
- `zoom`

The cost is concentrated in `do_steps`: batching a known sequence into one call
is the main thing that makes a long task cheap, and a prompt in front of it
gives that back.

## Two operational notes

Claude Code reloads settings files while it runs, so an edit to `permissions`
applies to the session in progress. The server is not like that: the process
Claude Code is holding open is whatever `mcp_server.py` was on disk when the
session started, so a change to the server is invisible until a restart, or
until you speak MCP to a fresh copy with `tests/mcpdrv.py`.

And the requirements in the README are not optional for a working setup. In
particular `toolkit-accessibility` must be true *before* an application starts,
or that application's tree is stunted in a way that reads as "this app has no
widgets".
