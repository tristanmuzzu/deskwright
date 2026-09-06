# Codex on Linux

Deskwright supplies native GNOME/Wayland control to Codex via MCP. It does not
activate the proprietary macOS/Windows Computer Use implementation on Linux.

Register a checkout (replace the path):

```bash
codex mcp add deskwright --env DESKWRIGHT_IMAGE_PROFILE=original -- python3 /absolute/checkout/mcp_server.py
codex mcp add deskwright_private --env DESKWRIGHT_SESSION=headless:codex --env DESKWRIGHT_HEADLESS_HOME=/absolute/private-home --env DESKWRIGHT_IMAGE_PROFILE=original -- python3 /absolute/checkout/mcp_server.py
```

Use `startup_timeout_sec = 60` and `tool_timeout_sec = 180` for each server in `~/.codex/config.toml`, to allow
cold private-session startup and bounded waits. Start a private session with an
isolated home before first use if restored personal app state is unwanted:

```bash
python3 -m deskwright.headless start --name codex --home /absolute/private-home
```

Install `skills/deskwright-codex/` in the Codex user skills directory. Reload MCP
servers/start a fresh Codex session if the current session's tool catalog does
not refresh. Verify `desktop_health` and a screenshot on each intended server.
The physical server drives the user's screen; the private server has separate
windows. Do not drive both concurrently as though they were the same desktop.

## Image profiles

- `legacy`: existing 1568 px/JPEG 75 behavior; default for compatibility.
- `balanced`: at most 1920 px/JPEG 90, with lower-quality fallback only when the
  inline byte limit requires it.
- `original`: native size, lossless PNG, original-detail MCP metadata. Oversized
  images return a crop suggestion rather than silently losing detail.

`DESKWRIGHT_IMAGE_PROFILE` chooses a server default; a tool's `image_profile`
overrides it. The returned coordinate note describes the actual displayed image.
The legacy width*height/750 token estimate does not estimate OpenAI usage.

## Continuous drawing

`pointer_path(target, points, duration_ms)` presses once, follows an arbitrary
polyline at a target 120 Hz, and releases. Duration describes motion time; arrival
takes 80 ms and final delivery takes 60 ms. Wayland may coalesce motion
events; 120 Hz requests do not promise 120 Hz application delivery. Direct D-Bus runtime
checks run about every 100 ms and can extend timing on a slow desktop. There is
no pressure/tilt support. An interrupted stroke may be partial; automatic retry
is refused in `do_steps`.

Run `benchmarks/run_strokes.py` on a named private desktop for an independent GTK
input witness, PNG evidence and measured MCP screenshot latencies. This is a
scripted integration test, not a model-performance score.

## Measured input recovery

GNOME 50 can discard the first input on a new virtual device. Pointer input now
reasserts its initial arrival; keyboard input initializes with one Shift tap
before the first requested key (110 ms once per input session). The private GTK
witness received 5/5 cold clicks and 5/5 cold keys after this change, versus 0/5
of each in the initial baseline run. A startup delay alone did not help keys.
These small samples establish a regression test, not universal reliability.

`ui_find` accepts `window_title` to search a specific accessible dialog and reports
when the node budget truncates a search. Automatic typing focus recovery stays
inside the named window, preventing a large background GIMP tree from stealing it.
