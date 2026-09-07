# Desktop execution and observations

The MCP supervisor serializes requests through a persistent worker. A call has
a 60-second wall-time deadline, including suspend. Individual waits inside it
are bounded to at most 50 seconds and the remaining deadline. Keep the client's
tool timeout above 60 seconds; the existing 180-second configuration is fine.

MCP cancellation stops the worker process group. Its compositor connection is
closed, releasing held compositor input. Supervised calls refuse the legacy
raw `ydotool` route because its external daemon can outlive a cancelled worker;
use compositor keysyms or AT-SPI. Direct unsupervised CLI use retains that route. This invalidates Python globals and observation
IDs. Errors report the known execution events and tell the caller to inspect
partial effects. The error retains the last 512 progress events and reports how
many earlier events were omitted. The journal records primitive outcomes.
Cancellation does not undo edits. The desktop itself and
application documents survive an ordinary worker reset; commands explicitly
spawned as worker descendants may also be terminated.

All ordinary tool calls, batch primitives and Python adapter calls share the
same execution gate. A per-session lease serializes separate Deskwright server
instances too. Physical and private desktops have distinct leases. Do not
attempt concurrent mutation through a legacy server that lacks this lease.

Required waits stop `do_steps` and `desktop.wait` when unmet. Standalone
`wait_for` still returns `met: false` for a condition timeout. A successful input
delivery is distinct from visual or saved-document correctness. Never retry an
unknown or partially applied action automatically.

## Observations

Existing coordinate arguments are desktop coordinates. `screenshot` and `zoom`
add an `observation` object when geometry can be verified. To use coordinates
from that returned image, pass its ID as `observation_id` on pointer input. The
server accounts for crop origin and actual separate x/y scale factors. It
rejects invalid points, changed window geometry, a different desktop or an
expired frame. At most 64 observations are retained per worker.

This detects window geometry changes, not arbitrary content changes inside a
window. Observe again after changing a document view, scrolling or zooming.
Full validation on mixed-scale multi-monitor hardware remains a separate test.

`observation_mode: compact` on acting tools suppresses the automatic look,
settling and captures. It preserves receiver checks. Explicit screenshots are
immediate; automatic looks support `settle_max_s: 0` for one immediate frame.
The default observation behavior is unchanged. Original PNGs remain the Codex
image profile; the legacy estimate is not an OpenAI token/cost measurement.

## Optional persistent Python

Set `DESKWRIGHT_ENABLE_EXEC=1` in a server's environment and restart it to expose
`desktop_exec`. Existing tools remain available. This runs with the worker's
OS permissions, within whatever boundary the host uses to launch it. It is not
a Python sandbox or a separate paid model/API loop.

The globals are `desktop`, `log(value)` and `display(observation)`. For example:

```python
snapshot = desktop.screenshot()
display(snapshot)
```

After observing an actual target, code can call:

```python
desktop.key("ctrl+s", target=known_window_id)
desktop.wait("window_exists", target="Save", timeout=10)
display(desktop.screenshot())
```

Helpers: `click(x, y, target=...)`, `move(x, y)`, `path(points, target=...)`,
`type(text, target=...)`, `key(combo, target=...)`, `wait(condition, ...)`, and
`sleep(seconds)`. `desktop.call(tool_name, **arguments)` exposes the existing
guarded tools. This is a small Deskwright API, not full PyAutoGUI compatibility.
Input is sequential and compact by default. Use explicit observations after
short coherent groups; do not batch through an unobserved layout transition.

Code is limited to 64 KiB, text output to 32000 characters, displayed captures
to four per call and primitives to 4096. These are ceilings, not recommended
batch sizes. `display` accepts a captured screenshot result; it does not draw
or load artwork. Python variables survive normal calls and ordinary exceptions.
`reset: true` explicitly clears them and observations. The execution deadline, cancellation
or worker loss also resets them and is reported to the caller. A shorter unmet
condition wait is an ordinary error and does not reset the worker.

Code and typed text are fingerprinted in the default journal. Primitive
outcomes are recorded so partial runs remain reviewable without storing typed
secrets. Opt-in text logging retains the existing journal setting.

## Verification

`tests/test_execution_contract.py` covers wait failures, partial retries,
clipboard failure, zero settling, crop transforms, stale/session rejection,
halt checks, persistent state and cancellation of an infinite Python call.
`benchmarks/run_contract_safety.py` requires a named private desktop and checks
actual receiver coordinates, mouse/key release, stale frames and worker loss.
These measure execution behavior; they do not establish model/native parity.
