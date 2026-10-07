# Real-application comparison

Prepared 2026-10-06; completed on the visible main desktop on 2026-10-07.
Both methods completed the original workflow with exact output and unchanged
originals. The original semantic-first run took longer. A later four-run retest
with the updated hybrid/input code is recorded below; its average favored the
hybrid, but the two individual pairs had different winners. The final query-based
comparison below subsequently won both pairs by about 48% elapsed time.

## Environment and prerequisite

Installed applications confirmed: Files (Nautilus), GNOME Text Editor, GNOME
Calculator, LibreOffice and Chrome. The first three provide a realistic local
workflow without accounts, messages, downloads or changes to personal documents.

The user chose logout/login. After their return, the physical compositor answered
with build 2026-10-06.1, InteractionState worked, and all four new semantic tools
were loaded. No automatic logout or guard bypass was performed. Both runs used
the physical desktop, the same applications and window sizes.

## Task: prepare a reviewed estimate across three applications

Start with a disposable folder containing a plain-text estimate, not a custom
application. Each condition gets its own identical copy.

1. Launch Files and navigate to the disposable folder.
2. Create a `Reviewed` subfolder through Files.
3. Open the supplied estimate in GNOME Text Editor through the file manager.
4. Use the editor controls to revise the heading/status and preserve all line items.
5. Launch Calculator and evaluate `(125*3+79*2)*1.2` (expected result 639.6).
6. Transfer the result into the estimate as `Total: EUR 639.60`.
7. Save As `Reviewed/estimate.txt`, interacting with the actual file chooser.
8. Return to Files, find the saved document, and rename it `approved-estimate.txt`.
9. Open its Properties dialog and inspect the filename/type; dismiss it.
10. Reopen the final file in Text Editor and verify the saved content.

The original estimate must remain unchanged. The final file must exist at the
correct path and contain the exact expected text. No deletion, messages, account
changes, application preferences or personal files are part of the test. Save and
close only the windows and files created for the comparison.

## Conditions and measurements

A first: use ui_snapshot/ui_inspect/ui_action/ui_wait, with explicit application
launch and window focus. Record every pointer/keyboard fallback where native
accessibility cannot complete the task.

B second: use screenshots to choose targets, pointer/keyboard input and ordinary
shortcuts. Do not use semantic snapshots, accessible names, old ui_find/ui_press,
DOM, application scripting or filesystem writes to complete the task. Existing
input-tool safety checks may internally use accessibility; this is not an
accessibility-free operating system. Final independent disk verification is
allowed in both conditions and is outside the interaction timing.

Use identical application versions, display resolution, source content and goal.
Allow short batches and shortcuts in both conditions. Do not force a screenshot
between every keystroke or otherwise handicap the baseline. Record separately:

- Observed end-to-end elapsed time (including agent/tool decision gaps).
- Sum of backend tool execution time, action count and observation count.
- Screenshots used for decisions, semantic observations, retries and fallbacks.
- Exact artifact success and any incorrect intermediate action.
- Launch/startup time and human pauses, so they do not masquerade as input speed.

A visible single A/B pair is a demonstration, not a stable performance benchmark.
A runs first at the user's request; B benefits from task familiarity and warmed
applications. State this limitation instead of claiming an unbiased universal
speed ratio. A failure is a result: retain evidence and diagnose the missing
capability rather than silently replacing the workflow with a synthetic form.

## Measured result

| Measurement | A: semantic first, with fallbacks | B: screenshots and input |
| --- | ---: | ---: |
| End-to-end elapsed | 582.06 s | 202.69 s |
| Sum of backend tool durations | 20.54 s | 20.55 s |
| Backend calls | 94 | 53 |
| Screenshots | 11 | 15 |
| Semantic observations (snapshot/inspect) | 28 | 0 |
| Native semantic action calls | 10 | 0 |
| Tool exceptions, inspected before recovery | 4 | 2 |
| Exact final file / original unchanged | pass / pass | pass / pass |

Raw event durations and counts: [JSON](../benchmarks/results/real-app-comparison-2026-10-07.json).
Backend durations time desktop.call locally; they exclude MCP transport, model
decisions, screenshot interpretation, event-log writes and explicit settling
delays. End-to-end includes those costs, first-run exploration and context
handling. There was no controlled repetition or counterbalancing. B benefited
from the A run's discoveries and an already running editor with restored tabs.
Window arrangement and launch costs are included in the call log. Neither the
2.87x wall-time difference nor equal backend totals is an intrinsic method speed
ratio. Four fewer screenshots did not translate into a faster completed task.

## Coverage and failures

Native exact text reads/writes worked in Text Editor and Calculator. Calculator
returned 639.6, which was transferred to the document. The native file chooser's
File Name entry and Save button worked after navigating to the target folder.
Properties exposed the expected filename and Plain text document labels.

The hybrid run required these input fallbacks:

- Files' Current Folder Menu opened a separate, unnamed compositor popup missing
  from the parent snapshot. A click guarded against the parent was correctly
  refused as occluded; the popup was observed and targeted explicitly.
- The embedded New Folder dialog produced a fresh reference that immediately
  failed inspection as stale. Snapshot stores the outer frame identity, while
  resolution compares the nearest frame/dialog ancestor. Keyboard entry and a
  visible Create button completed the step.
- File rows offered scroll-to but no usable semantic open/select operation.
  Opening files and entering the Reviewed folder used pointer input. Location
  navigation, Save As, rename and Properties also used ordinary shortcuts.
- Renaming used the visible popup and input, because popup targeting was already
  known to be incomplete. The initial click arrived before the button became
  usable; observation and a later click completed it.

Other findings:

- The initial broad snapshot exhausted its result budget on inherited actions
  attached to containers and labels. Role/name filtering helped; useful-control
  prioritization and subtree scoping still need work.
- Invocation acceptance did not mean application completion: Calculator initially
  read back the old expression, then displayed 639.6. Completion predicates must
  cover value changes, dialog appearance/disappearance and button readiness.
- Setting a slash-containing File Name did not provide folder navigation. The
  Save action was refused while insensitive. Setting a basename and entering the
  folder resolved it. This was an interaction mistake, not a bypass opportunity.
- Legacy type_text falsely reported verification failure for path replacements
  sharing a prefix, and for Calculator's visible conversion of `*` to `×`.
  Screenshots/readback confirmed correct input; no blind retyping occurred.

## Artifacts and cleanup

Each run used `/tmp/deskwright-real-app-comparison-20261007/<condition>/`.
Both produced `Reviewed/approved-estimate.txt`, 147 bytes, SHA-256
`5e3f7e9cd9b2621a44ab35002a6095893779ef12856f42f33b0d0bf0f10d876a`.
Both original drafts were checked against the exact original fixture. Final
documents were reopened through Files and verified in the editor; independent
disk checks followed the measured interaction. Screenshots were inspected live,
not recorded as a video. Only task tabs and Files/Calculator windows were closed;
the editor's restored personal tabs were preserved and ChatGPT regained focus.

## Remaining work

Fix embedded-dialog ancestry without weakening window identity guards; map
popups safely; provide guarded row selection/opening; prioritize useful controls;
add application completion waits; and make legacy typing verification account
for selected replacement text and explicit application normalization. Then repeat
this real-app workflow with several runs in alternating order. Windows remains
an unimplemented, untested backend; these Linux results do not validate it.

The MCP reconnect and GNOME logout/login directions were delivered before the
user's login. The running tools and new extension were subsequently verified, so
another reconnect or logout is unnecessary for this result.

## Updated main-desktop retest, 2026-10-07

Repeated the same ten-step workflow, twice per method, in A-B-B-A order. These
were agent-driven interactions on the visible desktop, including observation,
reasoning, tool transport and recovery time. They were not the prepared private
workflow script whose approximately eight-second runtime excludes agent time.

| Measurement | A1 hybrid | B1 visual | B2 visual | A2 hybrid |
| --- | ---: | ---: | ---: | ---: |
| End-to-end elapsed | 236.73 s | 195.16 s | 206.67 s | 129.04 s |
| Backend tool durations | 13.26 s | 13.03 s | 16.19 s | 14.94 s |
| Backend calls | 57 | 48 | 47 | 52 |
| Screenshots | 5 | 13 | 12 | 4 |
| Semantic snapshots / inspections | 15 | 0 | 0 | 12 |
| Native action calls (including rejected calls) | 9 | 0 | 0 | 8 |
| Tool exceptions | 2 | 0 | 0 | 0 |
| Exact final file / unchanged original | pass / pass | pass / pass | pass / pass | pass / pass |

Raw durations, counts and artifact hashes are in
[the retest JSON](../benchmarks/results/real-app-retest-2026-10-07.json).
Hybrid mean: 182.89 seconds (3:03). Visual mean: 200.91 seconds (3:21).
The hybrid took 9.0% less elapsed time on average in this demonstration. It lost
the first pair and won the second; this is not evidence of a consistent or
general 9% advantage. Two observations per method are insufficient for a stable
performance estimate. The much faster second hybrid run benefited from learned
timing and grouping semantic lookup, guarded action and verification in short
batches. Neither method was restricted to one action per agent call. No
production code changed between runs.

Both methods used the same updated typing code. Visual runs made zero semantic
snapshot, inspection, action or wait calls; their decisions used screenshots,
window metadata and keyboard shortcuts. The common input guard still checks
typing internally. Hybrid snapshots were scoped to useful fields/buttons, with
screenshots and ordinary shortcuts for file rows/navigation. Saved documents
were reopened through Files and checked in the editor before stopping the timer;
independent exact disk checks and cleanup happened afterwards.

Backend means were almost equal: 14.10 seconds hybrid versus 14.61 seconds visual.
Most elapsed time was outside the individual desktop operations. The useful
improvement was reducing agent observation/decision round trips through short,
verified batches, not making every native action intrinsically faster than a
click. Fewer screenshots alone did not guarantee the faster run.

The first hybrid run encountered an AT-SPI application-registration race just
after Files launched, and an agent call omitted the required current-text
precondition. Both were inspected and recovered; neither silently replayed
input. Its initial New Folder shortcut also preceded readiness, and Properties
was requested before rename completion. Later observations showed the actual
state before those shortcuts were repeated. These costs remain in its timing.
Snapshots immediately after native invocation sometimes captured a closing
dialog; action acceptance still does not establish application completion.
The old embedded-dialog ancestry and typing false-failure problems did not
recur during valid actions. Both visual runs and the second hybrid run had zero
tool exceptions and all four artifacts were exact.

The client-held MCP server still had old schemas. A separate fresh MCP process
loaded the current checkout and verified `compact`, `ready`,
`text_changed_from`, and `expected_after` before timing. Both conditions used
that same process and transport. Bridge preparation was excluded. No GNOME
logout was needed or performed; the existing loaded extension was retained.
This verifies the fresh benchmark process, not an update to the client's held
connection. The fresh process was stopped after the test.

Fixtures remain under `/tmp/deskwright-retest-g6sd4l9k/`, in one directory per
condition. All four final files have the original comparison's 147-byte length
and SHA-256. Only task tabs and task Files/Calculator windows were closed;
personal editor tabs remained open, and ChatGPT regained focus. The desktop's
unrelated transient overlay disappeared on its own during the runs.

The practical choice remains a hybrid: use semantic text and completion checks
when they remove round trips, and keep screenshots/shortcuts for simple visual
navigation. More compulsory semantic discovery would work against the measured
bottleneck. Startup readiness and application-completion checks are the main
remaining reliability improvements exposed by this retest.

## Exact queries and shorter agent batches, 2026-10-07

The next implementation adds `ui_query` / `desktop.query`: exact multi-control
lookup, optional text readback, readiness and disappearance predicates in one
worker call. Incomplete scans and ambiguous targets stop the batch; input is
never replayed. Filtered snapshots avoid fetching unrelated action tables.

A development pilot took 184.25 seconds and encountered two query timeouts. Its
two-second deadline and separate 1.5-second scan cap repeatedly discarded slow
partial scans. The implementation now gives a scan the remaining query budget
and defaults to five seconds, returning immediately when conditions hold.
The pilot is retained in [its own log](../benchmarks/results/query-abba-2026-10-07.json),
not pooled with the later implementation.

After fixing that issue, froze production code and repeated the same complete
task in A-B-B-A order with fresh identical fixture directories. All four were
agent-driven on the main desktop. Hybrid used five short agent batches per run,
with native observations between actions inside each batch. Visual used ordinary
shortcuts and grouped input, with screenshots at changing dialogs/layouts. Both
used the same current input backend and fresh MCP process. No filesystem writes
completed the task; fixture preparation and independent disk checks were outside
timing. No prewritten whole-workflow script supplied these headline times.

| Measurement | A1 hybrid | B1 visual | B2 visual | A2 hybrid |
| --- | ---: | ---: | ---: | ---: |
| Full elapsed time | 98.24 s | 189.77 s | 168.28 s | 87.03 s |
| Backend tool durations | 27.34 s | 23.16 s | 23.29 s | 27.44 s |
| Backend calls | 51 | 49 | 48 | 51 |
| Screenshots | 3 | 13 | 12 | 3 |
| Native query calls | 12 | 0 | 0 | 12 |
| Tool exceptions | 0 | 0 | 0 | 0 |
| Reopened GUI content / exact disk output / unchanged original | pass | pass | pass | pass |

Mean completion time: hybrid 92.64 seconds versus visual 179.03 seconds,
48.25% less elapsed time (1.93x completion speed). The hybrid won both individual
pairs by about 48%. Backend time was slightly greater for hybrid: the gain came
from fewer model/transport observation round trips, not fewer primitive calls.
The visual B1 run needed an extra observation during folder navigation; it
performed no erroneous input. All four outputs were 147 bytes with the same
SHA-256 recorded above. [Raw events and checks](../benchmarks/results/query-final-abba-2026-10-07.json).

This demonstrates a substantial repeatable advantage on this familiar workflow,
not on arbitrary applications. Two pairs, one task, learned control names,
ordinary workstation load and variable model/transport latency remain material
limits. A broader varied-task evaluation is required before claiming universal
consistency. The baseline remains the screenshot-and-shortcut policy documented
above; this does not establish superiority over every possible visual policy.

Separate private worker tests created three folders and calculated three
expressions with exact results. Alternating traversal samples on the same Files
tree measured median filtered-snapshot time falling from 0.756 to 0.473 seconds
(37.5% less), with identical match counts and complete scans. Those are backend
measurements, separate from the main-desktop agent timings.
[Private samples](../benchmarks/results/query-live-2026-10-07.json);
reproduction helper: `benchmarks/query_regressions.py:run`, optionally passing
`baseline` as a saved pre-change semantic module. The initial cold private launch
did not register Files on accessibility within five seconds; the warmed repeat
passed. Cold session startup remains distinct from steady application operation.

Final checks: 475 tests passed, with the same two pre-existing prose failures
(curly apostrophe in the repository skill and the outdated inline-runbook
expectation). Changed Python files pass Ruff; whole-repository Ruff still reports
the two pre-existing import-order issues in the private launcher. No extension
change or logout was required. The client-held MCP still needs reconnection to
load the new Python API.

After timing completed, the temporary benchmark bridge exited with SIGTERM
before cleanup. Results were already persisted. Cleanup used the existing main
MCP connection and verified only task windows/tabs were closed, personal tabs
and Chrome preserved, and ChatGPT focused. The private test desktop was checked
empty and stopped by its exact owned name.

## Four varied hybrid tasks, 2026-10-07

At the user's request, ran four additional agent-driven hybrid tasks on the
main desktop with frozen production code. These are different realistic work
items, not four repetitions of one task. Their average describes this small
mixed workload; it is not a new comparison with the earlier visual baseline.
Fixtures were synthetic and disposable. No prewritten whole-workflow script
completed the tasks, and no filesystem write created the final reports or moved
their files. Preparation and independent final disk checks were outside timing.

| Task | Elapsed | Backend | Calls | Screenshots | Interrupted tool batches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Travel: calculate trip costs and remaining budget, revise a draft, save separately, rename and reopen | 242.19 s | 45.67 s | 74 | 8 | 4 |
| Workshop: search notes for catering, calculate costs, update an action deadline, save and move the report into Reviewed, reopen | 152.35 s | 24.20 s | 57 | 6 | 1 |
| Purchase: read both pages of a PDF quotation in Document Viewer, compare delivered prices in Calculator, save and file a recommendation, reopen | 148.83 s | 34.82 s | 61 | 8 | 1 |
| Delivery: extract a ZIP in Archive Manager, read its packing list and draft, calculate credit and corrected invoice, save and file the reconciliation, reopen | 410.13 s | 60.88 s | 102 | 14 | 3 |

**Mean 238.38 seconds (3:58); median 197.27 seconds (3:17); range 2:29–6:50.**
All four exact final files passed independent byte checks; all source drafts
were unchanged, and both archive payloads matched. Each final report was closed,
reopened through Files and checked by exact native text readback plus a final
screenshot. Arithmetic results were EUR 742 / 58 remaining; EUR 486 / 14
remaining; supplier B EUR 213 versus A EUR 222, saving EUR 9; and a EUR 21
credit with EUR 159 corrected invoice.

Timing begins with the first application action and ends after reopened text
verification and final screenshot capture, before transport/review of that last
capture. It includes model decisions, discovery, bridge calls and recovery.
Backend time averaged 41.39 seconds. Ordinary workstation/transport variation
was uncontrolled, and the agent adapted its strategy as controls were discovered.
The R1 disk check overlapped early R2 measurement by approximately 0.05 seconds.
[Raw events, per-run checks and hashes](../benchmarks/results/hybrid-natural-four-2026-10-07.json).

These runs expose considerably more friction than the familiar earlier task:

- R1 used an unsupported window-title wait and an ignored double-click argument,
  then recovered after observing the selected file. Putting a full path into
  the Save dialog's File Name field did not complete saving. Two unmet waits and
  one native query timeout were retained. Separating directory navigation from
  the base filename completed the save.
- R2 queried a Save button while the existing filename made the button read
  Replace. The query timed out; a screenshot, new filename and ordinary click
  completed the save. This was an agent targeting mistake, not evidence that
  semantic saving is unsupported.
- R3 waited for the wrong window class (`org.gnome.Papers` rather than `papers`).
  The PDF was already open. Ctrl+End also failed to change its page; an observed
  thumbnail click worked. Both recovery costs remain included.
- R4 changed the archive chooser's Location text but did not commit navigation
  before Extract. The application extracted the two fixture files into Home.
  Show the Files exposed the actual destination. A proposed move was initially
  rejected by automatic approval review because the selected files were not
  established. Read-only byte comparisons against the fixture archive and a
  complete native scan proved exactly the two selected fixture rows; the guarded
  retry moved both through Files into R4. Neither fixture remains in Home.
  All approval-review and recovery time remains in the headline result. An
  oversized observation response and two native-query timeouts also occurred.

There were nine interrupted tool batches plus the separately counted approval
block. The low-level event list records five thrown tool exceptions; helper
wait failures (three) and the response-size failure (one) occur above that
instrumentation layer and are included in the per-run totals. No failed run or
slow recovery was discarded.

The result does **not** establish consistently wide hybrid superiority. The
highest-priority improvements are better agent use of the actual tool schema,
discovery of dynamic labels and window classes, explicit commitment and
verification of navigation, and earlier visual fallback when a complete native
scan is unreliable. Setting a text field and verifying its text does not prove
the application adopted that value as its navigation destination. Keep that
distinction in completion checks; do not weaken ambiguity or modal guards.

Only task tabs/windows were closed. Personal editor tabs and Chrome remained;
ChatGPT regained focus. The task bridge shut down normally. A pre-existing
utility window disappeared independently of our targeted actions. No extension
change, logout, production-code edit or new test-suite run occurred in this
measurement turn. Fixtures remain under `/tmp/deskwright-natural-k1e2zj1o/`.

## General hybrid routing and recovery, 2026-10-07

The follow-up changes address shared interaction costs, without application names,
benchmark labels or workflow recipes in production routing:

- `ui_observe` returns compact native controls or fallback pixels in one response.
  Unproductive native scans temporarily give way to direct capture; changed window
  identity/title/geometry or an explicit native probe allows reconsideration.
- Observed container references scope repeated exact queries. Identity, visibility,
  ambiguity, modality and halt checks remain. Completeness applies to that scope.
- Actual tool schemas are available inside the worker. Unknown arguments and
  invalid top-level choices are rejected before input instead of silently ignored.
- New windows are discovered by their IDs; title waits are explicit. Failed batches
  preserve partial-action status and useful query evidence, and attach fresh pixels
  when possible. No action is automatically replayed.
- Native text readback explicitly verifies field contents, not application-level
  navigation or saving. Failed direct launchers return early rather than consuming
  their entire arrival timeout; successful process handoffs continue waiting.

### Component measurements

All new live checks used a disposable private GNOME desktop. The four earlier
natural task measurements remain unchanged. These are backend component timings,
not a new end-to-end agent comparison or evidence of universal superiority.

| Measurement | Before median | After median | Reduction |
| --- | ---: | ---: | ---: |
| Repeated exact query: whole tree versus observed panel | 421.72 ms | 49.31 ms | 88.3% |
| Unproductive scan plus capture versus capture during cooldown | 352.24 ms | 170.27 ms | 51.7% |

Each row uses three samples per method in alternating A/B/B/A/A/B order on the
same unchanged scene. The [GTK fixture](../benchmarks/fixtures/hybrid_surface.py)
contains a four-node named editing panel and 400 unrelated rows. Panel discovery
is excluded from repeated-query timing; all queries returned the same exact text.
The baseline semantic module was retained before editing and its SHA-256 is in
the [raw results](../benchmarks/results/general-hybrid-live-2026-10-07.json).

The cooldown experiment deliberately constrains both native probes to 200 ms to
exercise an unproductive tree. It compares the old snapshot plus screenshot with
the adaptive observer; it does not compare against an agent that already chooses
screenshots directly. Its first adaptive probe cost 438.04 ms and is recorded
separately, not hidden inside the warm median. Default native-probe budget is
1,000 ms. No model or network round-trip time is represented by these medians.

### Live coverage and failures

Native observations worked in Files, Calculator, Text Editor and Archive Manager.
Calculator evaluated `(127+83)*1.2` to `252`. Text Editor's fixture was edited,
saved, closed, reopened and checked. Archive Manager's extraction modal was
discovered by new-window identity and cancelled without extracting anything.
Three later modal-open/observe/cancel cycles passed on the final code.

A separate GTK drawing surface received automatic pixel fallback. Loupe exposed
its toolbar as native controls but did not expose its main image with a drawing
role; explicit visual mode correctly returned the image. This is a measured
limit of automatic role detection, not proof that a complete native scan covers
all visible content. Agents must still select pixels for visual tasks.

A renamed button refused its stale reference, stopped the batch before its next
statement, and returned one fresh screenshot with `partial` batch status and
`not_started` for the rejected action. A deliberately short broad query also
returned its timeout and recovery screenshot. The scoped replacement succeeded.
The test selector had additionally guessed `push button`; discovery established
that this provider uses `button`.

Two disk assertions initially assumed that Text Editor's saved bytes equal its
native buffer. This editor writes an implicit final newline. The subsequent
checks explicitly distinguish the saved bytes and reopened buffer; no global text
normalization or save replay was added. The form also proved that assigning a
field does not trigger its commit action.

An initial observation of an unmapped dialog attempted an invalid zero-size crop.
The observer now captures the full desktop while geometry is zero, without input
or a fixed sleep. Unit checks cover all three observation modes for that branch;
the three final live dialog cycles were already mapped when observed.

Papers could not open this private display: a diagnostic launch exited with
`Gtk-WARNING: Failed to open display`. Its original failed call took 15.75 seconds.
After general failed-launch detection, the same failure returned in 1.57 seconds,
including recovery capture. This is a single diagnostic pair, not a repeated
latency benchmark, and does not fix Papers' display problem.

A cold accessibility probe took 5.70 seconds despite the cooperative scan budget.
The mapping shortcut avoids probing a known unmapped window, but an individual
provider call can still block beyond that budget. The worker's outer deadline
remains the hard bound. This implementation makes no universal subsecond promise.

The private desktop's 18/18 self-test checks passed. All owned windows were closed,
then the empty named desktop and its bridge were stopped. No physical desktop
input, logout or extension replacement was needed. Existing MCP connections need
a restart to load the Python changes. Windows behavior was not tested here.

Final verification: 134 focused tests passed; the full suite finished with 505
passed and two pre-existing prose failures (skill typography and the relocated
setup runbook expectation). Changed Python files pass Ruff and the final diff
passes whitespace checks. Repository-wide Ruff still finds two existing import
ordering issues in the private launcher script; these unrelated changes were
left intact. Details are recorded in the raw results and field notes.

## Autonomous improvement cycle (2026-10-07)

This cycle followed the request to continue finding, fixing and retesting useful
general problems without requiring another user prompt. It used one owned private
desktop, synthetic documents and fresh MCP workers. The physical desktop was
untouched. Extra desktop sessions were unnecessary for these serialized tests.

### Final comparison

Four executions per implementation, interleaved previous/new/new/previous/new/
previous/previous/new, edited a real Writer document through Find and Replace,
scoped native fields, Replace All, dialog disappearance, Save readiness, Ctrl+S,
an exact saved ODT check and a screenshot. The baseline is the hybrid code at
the beginning of this cycle. Both variants performed the same actions and checks.

| Implementation | Runs (seconds) | Mean | Median |
|---|---|---:|---:|
| Previous hybrid | 2.092, 1.900, 1.807, 1.769 | 1.892 s | 1.854 s |
| Updated hybrid | 1.016, 1.027, 0.992, 1.049 | 1.021 s | 1.022 s |

The mean worker-local duration fell **46.1%**, with all eight exact saved artifacts
passing. These timings exclude model decisions and MCP transport. They do not
establish a 46% agent-level gain or compare against screenshot-only computer use.
The earlier whole-task comparisons above remain separate evidence.

Reproduce using [provider_query_workflow.py](../benchmarks/provider_query_workflow.py)
in a private `desktop_exec` worker with a disposable open ODT and the retained
baseline module. [Raw results](../benchmarks/results/autonomous-hybrid-2026-10-07.json)
include every final run, exploratory trials, incidents and source hashes.
[The cycle-specific source diff](../benchmarks/results/autonomous-source-changes-2026-10-07.patch)
preserves the change relative to that baseline without relying on the repository's
pre-existing uncommitted changes.

### General changes and the regression they caught

Exact native queries can ask supporting AT-SPI providers for role matches before
reading only relevant controls. Fresh state, names, ancestry and ambiguity checks
remain mandatory. Unsupported providers and capped or failed searches use the
existing tree traversal; general discovery still scans for native and visual
content. No control or completion cache was added.

Exploratory lookup medians were 185 to 40 ms for the dense GTK fixture and 587 to
39 ms for Writer; GTK4 Text Editor stayed around 90 to 91 ms. Those early numbers
precede the final safety checks and are not the final implementation benchmark.

Testing Calc then exposed a regression in the initial optimization: a result cap
does not bound how much work a provider does looking for rare roles inside a
virtual spreadsheet. One failed batch took 10.07 seconds including recovery.
The final implementation first makes a capped unfiltered size probe. It found
2,114 nodes in Writer in 7 ms and hit the 4,001-node cap in Calc in 187 ms. Oversized
scopes skip role filtering and return to bounded traversal. Final single-scan
Calc queries remained incomplete at about 1.5 seconds, similar to the previous
tree path; the spreadsheet workflow correctly used keyboard input, native dialogs
and pixels. The final Writer comparison above includes this size guard.

The [AT-SPI Collection API](https://docs.gtk.org/atspi2/method.Collection.get_matches.html)
limits returned matches, not provider execution time. Neither the probe nor the
cooperative tree budget is a universal hard latency bound; provider calls can
still block until the outer worker deadline. Global AT-SPI timeouts were not
changed without a tested restoration and compatibility contract.

A separate stale-ancestry defect was reproduced: a mock control moved under a new
window still passed with a cached old parent. The old code invoked once; the fixed
code rejected before input. Root and ancestor caches now refresh during identity
and scope checks. Other fixes preserve parent refs for unnamed fields, return
ambiguous native action choices without invoking one, reject explicit non-object
MCP arguments, and attach recovery observations when a target window disappeared.

### Real applications and recovery

- Writer: heading and Unicode body text, Save As, exact ODT content, PDF export,
  close/reopen, scoped Find and Replace, and repeated exact saves. The PDF's text
  was independently verified and it opened in Draw; its initial screenshot was
  covered by a tip dialog, so that attempt is not counted as a completed visual
  PDF review. The PDF retains the earlier Friday wording; the final ODT says
  Wednesday, as expected after subsequent edits.
- Calc: a pasted table, observed Text Import modal, native formula-option readback
  and confirmation, native Save dialog, ODS save and reopen. Three multiplication
  formulas evaluated to 42, 24 and 12; SUM evaluated to 78. Saved formula/value
  pairs and the reopened sheet agreed. Its whole-window native tree is too large
  for a complete query; this is a successful hybrid workflow, not full semantic
  coverage of a spreadsheet.
- GIMP: observed welcome controls, native dismissal, explicit visual canvas
  observation, a continuous brush stroke, undo, and exact restored canvas pixels.
  After unmaximizing, a click using the old image was refused before input.
- GTK3 fixture and GTK4 Text Editor: comparative native lookup tests; the latter
  showed no Collection speed benefit. No artificial success was inferred from
  that neutral result.

Three cold Writer reopen checks passed. Launch now skips declared splashes and
requires mapped geometry in two consecutive polls, but GIMP still returned a
long normal-type startup window before its document window existed. LibreOffice
also used a temporary normal title. Fresh discovery and document postconditions
handled these cases; the result explicitly describes arrival rather than readiness.
Adding application-name sleeps or blindly repeating launches would be unreliable.

Papers' failed private launch was traced to AppArmor EACCES on the custom Wayland
socket. Its installed policy permits the usual wayland-[0-9] paths. The policy
was neither disabled nor bypassed, and Papers support is not claimed.

The [live receiver suite](../benchmarks/results/autonomous-contract-2026-10-07.json)
passed all 12 checks: crop/resize clicks and drags had zero-pixel error; cancellation
released mouse and keyboard; worker loss released a held mouse button and cleared
Python state; stale observations were rejected. The hard deadline released the
held key and reset state in 60.023 seconds. These are actual receiver events,
not assertions based solely on tool return values.

### Verification and stopping point

The complete local suite passed **527 tests** with one existing GLib deprecation
warning. Repository-wide Ruff is clean. The earlier prose and import-order
failures were repaired narrowly. All task applications were saved as appropriate
and closed before the receiver tests. The private self-test passed 18/18 checks.
The empty named desktop and its test worker were stopped; cleanup is recorded
in the raw results. Final whitespace checks passed.

The remaining larger improvements need different evidence: an actual model-host
end-to-end evaluation, platform support for confined applications, or a real
Windows UI Automation adapter and Windows runtime. More aggressive control
caching, automatic input retries, guessed native actions and weakened modal or
text guards would trade away verified correctness for uncertain speed. This is
the stopping point for this local optimization cycle, not a claim of perfection
or consistent superiority on every task. Fresh MCP workers load the Python
changes; existing connections need restarting. No new extension change or logout
is required for this cycle.
