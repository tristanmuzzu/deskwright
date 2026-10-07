# A semantic desktop interface for agents

Research and private-desktop experiments, 2026-10-06. Source revision:
`9624a33846d7d2b52599be67e5a06f4d8a30c5a6`. This is a research spike and a
proposed implementation sequence, not a released feature or a model benchmark.

Follow-up: the Linux semantic tools have now been implemented in this checkout
and exercised on both the physical demonstration and private validation desktops.
See [the current contract and validation](semantic-interaction.md). The measurements
below remain the original research baseline; Windows remains a proposed adapter.

## Recommendation

Build a compact, queryable, live view of application controls on top of native
accessibility interfaces. Let agents invoke the controls through code, batch
short verified workflows, and request images where semantics are missing.
Linux feasibility is demonstrated below. Windows has the necessary native
interfaces; a Windows backend still needs implementation and live validation.
Linux's native building blocks include [actions][atspi], provider-side
[Collection queries][atspi-collection] and [event listeners][atspi-events].

The practical target is broad coverage of forms, dialogs, navigation and text,
with explicit gaps. A universal conversion of every application's entire GUI
into a complete, reliable API is not achievable from accessibility alone.
Custom drawing, lazily created controls, missing labels and unavailable provider
methods prevent that promise. The percentage of real tasks covered remains
unmeasured.

Deskwright already has the foundation: `ui_tree`, `ui_find`, `ui_press`,
`ui_read_text`, `ui_set_text`, and a persistent `desktop_exec` with compact
observations. The opportunity is to make these easier to discover, cheaper to
query, more complete in their capabilities, and safer to combine.

## What was actually tested

All GUI work ran on this task's private GNOME desktop. No production extension
or server implementation was changed. Initial app setup and research time are
excluded from timings. Measurements are local, warm, scripted calls inside
`desktop_exec`; they exclude MCP transport, model inference, approvals and
reasoning turns. App versions observed: GIMP 3.2.2, LibreOffice 26.2, Chrome
152.0.7977.75. Raw samples and outcomes are in
[`semantic-gui-2026-10-06.json`](../benchmarks/results/semantic-gui-2026-10-06.json).

| Experiment | Observed outcome | Limit |
| --- | --- | --- |
| GNOME Text Editor | Created multilingual text, invoked the frame's native Save As action, filled the separate portal dialog, saved, closed and reopened; exact disk and widget checks passed | One document workflow; editor adds a final newline on save, which the first exact check caught |
| GTK3 form | Five fields filled, Save invoked, fields cleared, Reopen invoked, five fields read back; 5/5 exact UI and file checks passed | Synthetic fixture, existing Deskwright primitives |
| Chrome local page | Native accessibility action invoked the page's Save project handler; page changed to `Saved: Draft` | Its readable input lacked EditableText; direct text setting was correctly rejected |
| Canvas on same page | The painted words `Paint a star` were visible in pixels but absent from the complete 260-node accessibility scan | Canvas itself had a node and generic actions, but no useful name or painted-content semantics |
| Events | An AT-SPI text-change event arrived for the changed GTK3 field | One event/provider case, not proof of lossless event delivery |
| Provider queries | Window-scoped Collection queries worked in GTK3 form, GIMP and LibreOffice | GTK4 editor did not expose Collection; a GIMP application-root query timed out |

The editor and form workflows used no pointer or keyboard input. Images were
inspected later to verify visible outcomes and investigate coverage. A later
Chrome keyring/overview issue and its controlled reproduction required two
pointer cancellations and Escape; this recovery is
not included in the screenshot-free document/form claim.

### Measured speed and payload

Five complete form trials each used 17 guarded primitives and zero screenshots.
Median local execution was **2.282 seconds**. This is evidence that short code
batches can perform substantial GUI work without a model turn per primitive.
It is not a comparison against a model doing the same task visually.

For the same Save action and starting state, seven alternating paired trials
measured **1,267 ms with automatic visual checking versus 19.6 ms in compact
mode**. Each checked the saved trial identifier; the five full workflows separately
checked every field and the complete saved file. The final comparison ran with
the form focused and the overview dismissed; earlier overview samples are
retained separately. This isolates observation-policy cost on one control.
It does not imply a 64x end-to-end agent speedup. Both modes already use the
same accessibility action.

The read-only snapshot prototype collapses anonymous layout wrappers, preserves
useful ancestry, adds provider states/capabilities and labels, and keeps long
native addresses in a server-side table. It does not collect text values.

| App/state | Nodes inspected | Agent controls | Raw JSON bytes | Compact text bytes | Raw / prototype median acquisition |
| --- | ---: | ---: | ---: | ---: | ---: |
| Text Editor, saved document | 84 | 15 | 12,655 | 1,048 | 287 / 213 ms |
| GTK3 form | 28 | 19 | 3,920 | 1,202 | 36 / 37 ms |
| GIMP main window | 400, incomplete | 388 | 55,986 | 31,864 | 471 / 368 ms |
| LibreOffice Writer | 400, incomplete | 383 | 65,743 | 40,344 | 535 / 523 ms |

Seven paired samples for editor/form, five for GIMP/Writer. Both scans use a
400-node/depth-30 bound, but collect different properties and may visit different
subsets when capped. These are view-design measurements, not equivalent-output
benchmarks. Bytes are UTF-8 payload sizes, not billed tokens. The editor view
shrunk about 92%; the form collector did not become faster. Large menu trees
show why simply dumping and compressing an entire application is insufficient.

A window-scoped Collection query retrieved the form's three button names in
about 1 ms median across seven samples. That query returns much less information
than a snapshot. It supports trying provider-side selection, not claiming a
like-for-like speedup. Query capabilities and errors must be checked per scope.

## Findings that change the design

1. **Accessibility invocation is not physical clicking.** It can run in an
   unfocused or obscured app. This is valuable, but it does not reproduce the
   full mouse-down, hover, focus, key-event and gesture sequence. Some software
   depends on those events. Verify application state and retain physical input
   as an explicit fallback.
2. **Shell modals need a separate guard.** Chrome requested a private keyring
   password while the overview was visible. The page's semantic Save action
   still ran behind it. Compositor hit-testing reported no application receiver
   at the modal, even though window geometry/focus metadata named Chrome.
   A controlled repeat captured the prompt before and after invocation and
   verified the page changing from `Not saved` to `Saved: Draft`. The before/after screenshots are retained locally and are not included
   in the public release. No credentials were entered; the prompt was
   cancelled. A future semantic
   action layer must account for compositor grabs, modal scope and lock state,
   rather than equating a valid accessibility object with permission to act.
3. **Provider states need interpretation.** Working GTK4 editor controls
   reported ENABLED=false and SENSITIVE=true. The prototype initially called
   them disabled; it now exposes the raw bits. A universal boolean derived from
   one flag would reject useful controls or misrepresent them.
4. **Capability presence is not action success.** Chrome exposed Text but not
   EditableText on the input. Its action names were empty strings. GTK4 exposed
   large inherited action lists even on labels. Choose operations by tested
   capabilities and verify the result; do not count every advertised action as
   a useful agent control.
5. **Dialog ownership crosses processes.** The editor's Save As dialog belonged
   to `xdg-desktop-portal-gtk`, not the editor. Its blank-name filename entry had
   a LABELLED_BY relation to `Name`. Name-only search misses useful context.
6. **Transient addresses are not stable identities.** The editor changed its
   window title after text entry and an old expected-name action was refused.
   That is useful protection. However, child-index paths, substring names and
   roles cannot distinguish every replacement control or same-name sibling.
7. **A successful widget write is not a saved artifact.** The editor's extra
   final newline failed the first disk comparison despite correct widget
   readback. Save completion and reopening are separate verification steps.

The snapshot's `incomplete=false` means no traversal limit/error was recorded;
it does not prove the provider exposed every visible control or all app data.

## Proposed interface and ownership

The agent should first receive a small view such as this illustrative example:

```text
window w7 "Export document" revision 42
  e1 text "Filename" value="report.pdf" capabilities=[set_text]
  e2 checkbox "Include notes" checked=true capabilities=[set_checked]
  e3 button "Export" capabilities=[invoke]
  visual_regions=[preview]
```

It can then submit a bounded sequence: set e1, ensure e2 is checked, invoke e3,
and wait for an explicit completion condition. A newly opened dialog ends the
sequence unless the code observes and validates it before continuing. The API
above is proposed, not implemented by this spike.

Use three cooperating routes:

- **Native app or browser APIs** when they express the requested operation and
  its semantics are acceptable: existing browser tools, document APIs, app
  scripting. These can do higher-level work than manipulating individual fields.
- **Native accessibility** for controls and text: AT-SPI on Linux, UI Automation
  on Windows. Both backends produce the same modest semantic contract.
- **Images and guarded input** for missing controls, canvases, spatial operations,
  drag/hover behavior and visual verification. OCR/parser detections remain
  visual evidence; they do not acquire native control identity by becoming text.

Keep normalization, reference lifetime, scoped queries and state comparison in
one semantic service. Platform adapters own native discovery/actions/events.
Existing execution supervision owns serialization, cancellation, halt handling,
partial-result reporting and journaling. Avoid a second unguarded executor.
The existing primitives remain compatible while an additive API is tested.

Production references should be opaque and bound to session, process lifetime,
window and snapshot generation, with the native object identity retained where
available. Revalidate identity, requested capability, scope and relevant state
immediately before mutation. Use fresh exact selectors when the reference is
stale; reject ambiguity. Native runtime IDs themselves are not eternal IDs.
Backend restarts invalidate all references. Events invalidate cached regions,
but freshness checks and bounded polling remain necessary when events are lost.

Add typed operations only where a provider supports them: invoke, set_text,
set_value, select, set_checked, expand/collapse, scroll and read_text/ranges.
An action result distinguishes accepted, verified, failed and unknown. Unknown
or partial non-idempotent actions must not be retried automatically. Check final
values and document state, not just a successful native method return.

UI text remains untrusted data, including names and labels. Password values
should be omitted. Semantic access is not authorization to operate hidden
controls, approve system prompts, or bypass application/OS security boundaries.

## Windows route

Microsoft UI Automation already supplies a tree, control patterns, property
queries, events and bulk caching. Invoke, Value, Toggle, SelectionItem,
ExpandCollapse, Scroll and Grid cover much of the proposed contract. TextPattern
is not a general text-write API; the correct write mechanism depends on the
control. Virtualized items may require ItemContainer/VirtualizedItem realization
before they exist as usable elements. [Control patterns][uia-patterns],
[caching][uia-cache], [virtualized items][uia-virtual].

Prototype with pywinauto's UIA backend, or a small C# worker using FlaUI/UIA3.
For a maintained backend, a native worker with explicit cache requests and event
ownership is a reasonable candidate, not yet a measured winner. UIA calls and
subscriptions should live on the appropriate dedicated COM MTA thread; hung
providers need an outer process deadline. [pywinauto][pywinauto], [FlaUI][flaui],
[Microsoft threading guidance][uia-threading].

Share the semantic contract, not GNOME implementation details. Window discovery,
input, screenshots, session isolation and packaging need Windows implementations.
Run the worker inside the intended interactive session; do not assume a GNOME
private desktop maps directly to a Windows virtual workspace or service session.
UAC, elevated apps and protected desktops are explicit boundaries, not something
an accessibility adapter automatically bypasses. [Security model][uia-security].

No live Windows backend was tested in this investigation. Required next proof:
Notepad, File Explorer, a WinUI/WPF form, Chromium/Electron, a custom canvas and a
virtualized list; include modal dialogs, duplicate names, stale references,
wrong-window refusal and provider hangs. Compare matched cross-platform apps
and task fixtures before drawing platform performance conclusions.

## Existing work and the opportunity

This direction has strong precedent. Playwright MCP exposes structured browser
accessibility snapshots. Microsoft's UFO2 combines Windows UIA, vision and app
APIs, with multi-action planning to reduce model overhead. OSWorld supports
accessibility observations alongside screenshots. OmniParser derives structured
screen information from images, which is useful fallback work but still pays
image-processing costs. [Playwright MCP][playwright], [UFO2][ufo],
[OSWorld][osworld], [OmniParser][omni].

The opportunity for Deskwright is reliable cross-application execution, concise
observations, explicit coverage/fallback, and consistent Linux/Windows behavior.
The novelty cannot be simply exposing an accessibility tree; Deskwright and
other tools already do that.

## Implementation order and acceptance gates

1. **Compact snapshots and scoped queries.** Add relations, capabilities,
   explicit coverage limits and opaque references. Collapse wrappers, separate
   app commands from visible controls, and support subtree expansion. Preserve
   current tools. Gate: resolve requested controls on the small test matrix,
   reject wrong instances/stale siblings, and retain every required task control.
2. **Safe semantic actions and verification.** Establish modal/lock/grab policy,
   typed operations and exact readback before making semantics the default.
   Existing `ui_set_text` has a fixed 200 ms wait per write; test predicate/event
   completion before replacing that wait. Gate: successful real saved/reopened
   tasks, plus refusals and honest partial outcomes under injected failures.
3. **Incremental observation.** Subscribe to scoped events, coalesce changes and
   fetch only relevant state; re-snapshot after loss, restart or budget overflow.
   Gate: noisy/missing-event and provider-stall tests, no stale action permitted.
4. **Windows adapter.** Prove the shared contract in a real Windows session and
   run the same failure suite. Keep platform-specific capabilities explicit.
5. **Controlled model evaluation.** Compare current pixel-oriented usage,
   current compact semantic tools, and the new interface with the same model,
   effort, app versions, task reset and verification oracle. Record wall time,
   model turns, image count, repairs, correctness and wrong-target attempts.
   Set a product goal such as halving median task time with no correctness
   regression; treat it as an acceptance target, not a forecast.

Total task time includes model decisions, discovery, actions, waiting and
verification. The local experiments isolate useful costs, but only the final
model evaluation can establish the overall advantage.

## Reproduction and validation

See [`semantic_snapshot_probe.py`](../benchmarks/semantic_snapshot_probe.py).
Inside an existing private `desktop_exec`, load the file with `runpy.run_path`,
then call `compare(desktop, "gnome-text-editor")` or `snapshot(app)` followed by
`render(snapshot)`. `collection_buttons(app)` uses the first accessible window;
choose an appropriate app state before calling it. It is an optional query path.

Launch `benchmarks/contract_form.py /tmp/your-run/form.json` on that same private
desktop, inspect its five fields/buttons, then call
`exercise_form(desktop, "/tmp/your-run/form.json")`. This function deliberately
edits the disposable fixture. Focus it and dismiss any overview before measuring
visual observation cost. Do not reuse this runner against a user's document.
The browser fixture is `benchmarks/fixtures/semantic-coverage.html`; the probe
used a separate Chrome profile, `--force-renderer-accessibility` and
`--ozone-platform=wayland`. The first launch without explicit Wayland timed out;
its cause was not established. No machine-wide browser settings were changed.

The prototype passes its scoped Ruff check. The full repository run produced
390 passing tests and two failures in already modified guidance files; the
full Ruff run found two import-order errors in an existing untracked launcher.
The six checks covering changed documentation also passed. All owned app
windows were closed and the private session was stopped. These unrelated files
were preserved. The findings ledger records the exact
locations. No release, commit, push or extension reload was performed.

[atspi]: https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Action.html
[atspi-collection]: https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Collection.get_matches.html
[atspi-events]: https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.EventListener.html
[uia-patterns]: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview
[uia-cache]: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-cachingforclients
[uia-virtual]: https://learn.microsoft.com/en-us/windows/desktop/WinAuto/uiauto-workingwithvirtualizeditems
[uia-threading]: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading
[uia-security]: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview
[pywinauto]: https://pywinauto.readthedocs.io/en/latest/getting_started.html
[flaui]: https://github.com/FlaUI/FlaUI
[playwright]: https://github.com/microsoft/playwright-mcp
[ufo]: https://arxiv.org/abs/2504.14603
[osworld]: https://arxiv.org/abs/2404.07972
[omni]: https://github.com/microsoft/OmniParser
