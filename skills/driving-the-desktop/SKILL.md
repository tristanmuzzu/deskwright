---
name: driving-the-desktop
description: Operate native GNOME Wayland applications with Deskwright, using accessibility, pointer input, screenshots, and private desktops.
---

Read `desktop_health` at the start to confirm the desktop and available capabilities. Keep the workflow on the intended server. `DESKWRIGHT_SESSION=headless` or `headless:<name>` selects a private desktop with separate applications, not the user's open windows.

Choose observations for the task. Accessibility can address controls and read or set text; scope `ui_find` by app and exact window title in large applications. Verify the resulting state. Widget paths and screen-map refs can become stale. Browser tooling can be useful for ordinary web UI when available. Canvas work needs visual inspection and pointer input; OCR can help when a toolkit exposes little accessibility information. Neither accessibility nor pointer delivery proves that an application performed the intended operation.

Batch short coherent sequences with `do_steps`. Wait for actual controls and inspect a newly opened dialog before entering data into it; window existence can precede painting. Unmet required waits stop the batch. A failed action may have partially applied: inspect before retrying. Read back small text changes because a visual-change threshold can miss them.

Use `pointer_path` for continuous marks: observed target window, desktop-coordinate `points`, and `duration_ms` from 50 to 15000. Up to 2048 vertices are accepted; pen pressure and tilt are unavailable. Window bounds are not canvas bounds. Dabs and paths are both useful; choose from the task and observed output rather than assuming a model-training preference. Save/reopen the deliverable and inspect an export for artwork.

When offered, `desktop_exec` runs persistent Python with `desktop`, `log`, and `display`. `display(desktop.screenshot())` returns captured pixels. `desktop.call` uses the same guarded tools; helper input is compact by default. Variables survive ordinary calls and errors. The 60-second execution deadline, cancellation, or worker loss resets variables and observations; a shorter unmet condition wait is an ordinary error. Code inherits the worker's host permissions and is not a separate sandbox.

Screenshots/zoom return observation IDs when geometry is available. Pass `observation_id` for image-relative pointer coordinates; otherwise coordinates are desktop-relative. Crop origin and x/y scaling are transformed by the server. Geometry changes invalidate frames. Content changes can also make a target stale, so observe after scrolling or changing a document view. Explicit screenshots are immediate; wait for readiness when needed. `observation_mode: compact` skips automatic captures while retaining input guards.

The human halt switch is `Super+Ctrl+Escape`. If engaged, stop and report it. `journal` provides the action trail, with text/code fingerprinted by default. Cancellation stops input; it does not undo edits. Separate scripted tool timings from autonomous model completion, token usage, and comparisons with native Windows/macOS.
