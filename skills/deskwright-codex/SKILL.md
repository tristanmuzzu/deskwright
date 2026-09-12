---
name: deskwright-codex
description: Operate native Linux GNOME applications with Deskwright, including visual editing, continuous drawing, and private desktop workflows. Use for Linux desktop tasks when Deskwright tools are available.
---

Use the available Deskwright MCP server for native Linux work. `deskwright` controls the physical desktop; `deskwright_private` controls the private desktop. Read `desktop_health` at the start and confirm the named desktop. Keep a workflow on one server. A private desktop has its own app session; it does not contain the user's open windows.

Choose observations for the task. In large apps, scope `ui_find` with `app` and the exact `window_title` from `list_windows`; heed truncation notes. After opening a dialog, wait for its controls to appear before batching dependent text entry. Accessibility (`ui_find`, `ui_read_text`, `ui_press`, `ui_set_text`) works well for controls and text. Visual work needs screenshots: inspect the canvas and use pointer input. Prefer the installed browser tools for ordinary web UI when available; use Deskwright for native applications and desktop integration.

Images default to `original` in this Codex setup: lossless native pixels, with original-detail metadata. A noisy large screen can exceed the inline limit; crop with `zoom` or use `image_profile: balanced`. Follow the returned coordinate note, especially for crops and scaled screenshots. The legacy token estimate is not OpenAI billing information.

Use `pointer_path` for a continuous brush stroke: pass `target` and `points: [[x,y], ...]` in desktop coordinates, plus `duration_ms` (50–15000, default 1000). It holds one button through up to 2048 vertices. Fit points to the observed canvas; window bounds are not canvas bounds. The tool checks window identity, geometry, occlusion and halt periodically. It releases the button on failure. It has no pen pressure or tilt. Read the resulting image; input delivery alone does not prove good artwork.

Use `do_steps` for known sequences, including `do: path`; it validates the sequence before input and captures the final state. Do not automatically retry a failed stroke, since part of it may already be painted. Inspect, then undo or repair if appropriate. Use persistent tool sessions and batch coherent work; do not batch through an unobserved dialog or layout change.

A new window may briefly report zero geometry. Use the updated window returned by `activate_window`, or observe after it is mapped, before planning coordinates. Private session setup overrides inherited X11 settings and preserves the host session registry for child processes.

Verify the actual deliverable: save and reopen an edited document, inspect/export artwork, or read back the affected app state. Separate scripted tool timings from model task completion and from native Windows/macOS comparisons. `Super+Ctrl+Escape` is the human halt switch; if engaged, stop and report it.

Small text edits can fall below the visual-change threshold. Read back the widget before retrying; `look.visual_change_detected: false` does not prove an action failed. `type_text` never probes focus with Tab, since that can indent the document. Prefer `ui_set_text` when GTK reports unreliable widget focus. In the measured GNOME Text Editor popover case, an explicit Ctrl+Tab restored focus before typing. Use that only after inspecting this app's state; Ctrl+Tab has different meanings in other applications. An unchanged document alone does not prove a failed typing attempt had no effect: menu shortcuts or another field may have received it. Inspect before any retry.

When available, `desktop_exec` runs persistent Python with `desktop`, `log`, and `display`. Use `display(desktop.screenshot())` to observe. `desktop.call` uses the same guarded tools; helper actions are compact by default. Variables survive ordinary calls/errors; cancellation, the execution deadline or worker loss invalidates them. A shorter unmet condition wait is an ordinary error. Keep calls below the 60-second deadline. Ordinary tools remain valid; choose based on observed task efficiency.

Screenshots/zoom return observation IDs when geometry is available. Pass `observation_id` with image-relative pointer coordinates and let the server transform them. Without that ID use desktop coordinates. Geometry changes invalidate frames, but content changes can also make targets stale: observe after scrolling, changing canvas zoom or opening a dialog. A failed required wait stops a batch; inspect any partial action before retrying.

For standalone window focus and arrangement, call `activate_window` or `window_layout` directly. `window_layout` cannot close windows; keep `window_manage` for closing. Batches and `desktop_exec` retain their own approval checks. Use a separately named desktop for concurrent benchmarks because the shared private registration is not isolated per task.
