# Practical desktop comparison

Keep raw actions and screenshots. A scripted tool test does not measure model task success. A debug run with code edits is not a timed model benchmark.

## Control tests

- `run_strokes.py OUTPUT`: independent GTK receiver records button events and draws the delivered path. Measures continuous corners, ellipse and wave, endpoint errors, distance to the received polyline, and five screenshots per image profile. Use a fresh output directory and a named private session.
- `run_recovery.py OUTPUT [BASELINE_SERVER]`: five fresh MCP connections per build, checks actual first-click down/up and first-key delivery, then moves a window during a stroke and checks that it stops and releases.
- `drive.py OUTPUT`: persistent MCP bridge for an agent-directed experiment. Stores calls and image evidence; contains no task-specific solving logic.

## Comparable model tasks

Run at least three fresh attempts per platform, same model/reasoning settings and application version, same display size and scaling, reset app state between trials. Record task wall time, model turns, tool calls, repairs, completion and artifact validity. Run the Windows VM and Linux benchmark sequentially for scored trials; VM overhead must remain visible in the report.

1. **Drawing and export**: In GIMP, create an 800×500 white canvas. Draw a dark blue mountain ridge with three peaks, snowcap details, a horizon and water lines. Add an orange circular sun and a smooth orange wave. Use the GUI brush tools. Save editable XCF and export PNG. Close and reopen the XCF, inspect the result. Success requires the right dimensions, recognizable requested shapes and colors, and valid saved files. This is a visual editing/control task, not a photorealistic painting benchmark.
2. **Document revision**: In a native editor, open a supplied project brief, revise a specified paragraph, add an action list, save under a new name, close and reopen, and verify exact requested text. Compare the same cross-platform editor for scores; GNOME Text Editor's Linux integration suite is a separate validation.
3. **Recovery**: During an editing workflow, introduce a renamed dialog or move a test window. Success requires detecting the changed state and completing without editing the wrong document. Record the intervention time and any repair.

The September 6–7 GIMP run was an exploratory debugging run and must be reported as such. Its saved artifact demonstrates end-to-end GUI work, but its elapsed time includes code changes, VM setup, documentation work and waiting. The authenticated Windows native Notepad capability probe passed on September 7. Its app-reported 7m13s includes cold setup and permission time. The substantive Windows GIMP and Notepad pilots are finished; controlled cross-platform scores remain unmeasured.

## Document task fixtures

Use an untouched copy of `fixtures/project-brief.txt` and give the model only
`fixtures/document-task.txt` plus the editor and output-directory names. Keep
`fixtures/expected-revision.txt` out of the model task context. After the trial,
run `python3 benchmarks/verify_document.py OUTPUT --source SOURCE` to check the
saved revision and unchanged input. The oracle accepts UTF-8 BOM and platform
line endings; reopening must still be verified in the actual application.

Each scored trial must record platform, model/reasoning, app/version, display
size/scaling, start/end monotonic times, supplied prompt, model/tool counts,
repairs, output paths, file checks and the screenshot after reopening. Missing
measurements stay missing. Do not label synthetic oracle checks as task passes.

## Drawing task fixtures

Use `fixtures/drawing-task.txt` unchanged with each trial's output directory.
`verify_drawing.py DIRECTORY` checks PNG decoding, dimensions, exact requested
colors and the XCF header. It does not score recognizable shapes or prove GUI
reopening; record those observations separately. The original Linux exploratory
artifact passes these file checks, which does not turn that debug run into a
controlled model benchmark.

The Linux document pilot also completed save-as and GUI reopening with its source
unchanged and an exact saved-file match. It exposed UTF-8 truncation, a mutating
Tab focus probe, and overconfident visual-change wording; fixes were made during
the run, so it remains a debugging pilot rather than a scored attempt.

## September 7 functional pilot outcomes

These are exploratory outcomes, not a controlled platform ranking. Full evidence
lives in the parent workspace's `evidence/` directory.

| Task | Linux / Deskwright | Native Windows VM |
|---|---|---|
| GIMP drawing | XCF/PNG saved, reopened, file checks passed | All requested shapes/colors and valid XCF/PNG; correct names in wrong folder; parent stopped after 26m48s of prolonged placement recovery; native reopen not verified |
| Document revision | Exact saved text, source unchanged, GUI reopening verified after fixing tool defects | Correct content/path, source unchanged, GUI reopening observed; two extra blank lines fail the fixed exact-text oracle |
| Input recovery | Independent cold-input receiver and moved-window interruption verified | Natural dialog, clipboard and filename recovery observed; no matched forced-interruption score |

The Windows document app reported 8m52s including setup and its app grant. The
drawing attempt included six app grants and a parent setup dialog; it was stopped
as an exploratory decision, not at a predeclared scored timeout. Parent artifact
collection does not count as native task completion. Its saved XCF was also opened
independently in Linux GIMP with all shapes intact.

The VM used four vCPUs, 4 GiB RAM and no accelerated GPU. Linux debugging ran on
the host during parts of the trials. Different editor applications, display
heights and setup histories prevent fair speed or reliability comparisons. The
three-trial protocol above remains the method for any future scored comparison.
