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
`describe(tool_name)` returns its current input schema locally. `observe(window_id)`
returns compact native controls or attaches a fallback screenshot in the same
response; log the returned metadata. `query(window_id, within=ref, **selectors)`
can restrict repeated exact lookups to a previously observed container.
Input is sequential and compact by default. Use explicit observations after
short coherent groups; do not batch through an unobserved layout transition.

Code is limited to 64 KiB, text output to 32000 characters, displayed captures
to four per call and primitives to 4096. These are ceilings, not recommended
batch sizes. `display` accepts a captured screenshot result; it does not draw
or load artwork. Python variables survive normal calls and ordinary exceptions.
`reset: true` explicitly clears them and observations. The execution deadline, cancellation
or worker loss also resets them and is reported to the caller. A shorter unmet
condition wait is an ordinary error and does not reset the worker.

Unknown argument names, missing required fields and invalid top-level enum choices
are rejected before input in normal MCP calls and `desktop.call`. Error details
include the actual schema. This prevents silently treating `clicks=2` as a single
click when the tool requires `count=2`. Existing handler validation still applies.
Explicit non-object arguments, including empty lists and null, are rejected;
only an omitted arguments object defaults to an empty object.

After selected targeting, verification or timeout errors, a stopped Python batch
attaches a fresh screenshot and window inventory when time and image capacity
remain. It observes the current focused window without activating it or replaying
any input. Original errors and unmet-condition evidence survive capture failures.
Earlier mutations make the batch status `partial`; the error also retains its
own action status. Human halt never triggers recovery. Arbitrary Python errors
are reported without automatic screenshots. Inspect all partial effects before
continuing; a screenshot is evidence, not an automatic retry instruction.

Direct application launches stop waiting if the launcher exits unsuccessfully
before arrival is confirmed. The error includes its PID and exit status; it does
not claim that no child process or other side effect exists. Successful launcher
exit can be a normal handoff, so observation continues in that case. No launch is
automatically repeated.
Window confirmation skips declared splashes and requires positive geometry in
two consecutive polls. This filters brief startup transients; it cannot establish
application readiness. GIMP and LibreOffice can expose normal-type startup
windows. Observe the intended document or controls before sending input.

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

## Choosing a fast interaction route

Minimize agent round trips, not just screenshots. Use a known keyboard shortcut
when it directly achieves the observed task. Use native reads and exact text
replacement for exposed fields and bulk text; use pixels for layout, canvas work,
unnamed popups or controls without a reliable native action. An unsuccessful
bounded lookup should lead to an appropriate fallback, not repeated tree scans.
No mandatory accessibility scan precedes ordinary computer use.

`desktop_exec` can combine a known action, its completion predicate and the next
observation in one call. `ui_wait` supports exact text, changed text and visible,
sensitive, non-busy controls; `wait_for` handles compositor window transitions.
Changed text is an observation, not proof of success: inspect its returned value.
Do not replay input because a verification result is late or unavailable.

`type_text` now reads the selected range/caret and pins the native object for
verification. It handles shared-prefix replacements and selected trailing path
completion. For a known app transformation, `expected_after` specifies the exact
full post-input buffer without changing the typed characters. For example,
Calculator can receive `3*4` with `expected_after='3\u00d74'`. This is explicit;
there is no global normalization that silently accepts different text.

Keysym typing defaults to 8 ms per character, with exact readback where available;
explicit ydotool retains 20 ms. Increase `key_delay_ms` for slower applications.
Private editor trials confirmed ASCII multiline input at both speeds; Unicode
keysyms were dropped by that session's keymap even at 20 ms. Prefer guarded native
text replacement for Unicode. The failure is reported, never automatically
retyped through another backend.

Current OpenAI guidance recommends code execution for GPT-6 Astra computer use
and supports existing custom UI/MCP tools. Deskwright already has that execution
shape. Preserve screenshot input and familiar mouse/keyboard operations alongside
native operations; do not force all work through a semantic abstraction. The
published integration guidance does not reveal Astra's training mixture, establish
an optimal screenshot frequency, or predict future models. Measure those choices
on the actual tasks instead of treating training claims as a benchmark.
[Official computer-use guide](https://developers.openai.com/api/docs/guides/tools-computer-use).

Keep task guidance short and conditional. OpenAI's Astra guidance warns that
elaborate recipes and excessive context can hinder stronger models; this does
not justify removing runtime guards or suppressing outcome verification.
[Official skills and prompts guidance](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra).
