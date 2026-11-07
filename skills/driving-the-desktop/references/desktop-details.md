# Desktop details

## Drawing and images

Claude's servers default to the `balanced` profile (up to 1920px JPEG), within the 2576px current Claude models read natively. Request `image_profile: original` (lossless PNG, no resize) when fine detail matters. Choose `image_profile: balanced` for broad overviews when fine detail is unnecessary, then original-detail `zoom` crops for text, alignment, and precise input. Inspect final visual deliverables at the fidelity their requirements need. A noisy large screen can exceed the inline limit; use a smaller original crop or a suitable balanced overview. Follow the returned coordinate note, especially for crops and scaled screenshots. 

Use `pointer_path` for a continuous brush stroke: pass `target` and `points: [[x,y], ...]` in desktop coordinates, plus `duration_ms` (50–15000, default 1000). It holds one button through up to 2048 vertices. Fit points to the observed canvas; window bounds are not canvas bounds. The tool checks window identity, geometry, occlusion and halt periodically. It releases the button on failure. It has no pen pressure or tilt. Read the resulting image; input delivery alone does not prove good artwork.

Use `do_steps` for known sequences, including `do: path`; it validates the sequence before input and captures the final state. Do not automatically retry a failed stroke, since part of it may already be painted. Inspect, then undo or repair if appropriate. Use persistent tool sessions and batch coherent work; do not batch through an unobserved dialog or layout change.

## Focus and recovery

A new window may briefly report zero geometry. Use the updated window returned by `activate_window`, or observe after it is mapped, before planning coordinates. Private session setup overrides inherited X11 settings and preserves the host session registry for child processes.

Small text edits can fall below the visual-change threshold. Read back the widget before retrying; `look.visual_change_detected: false` does not prove an action failed. `type_text` never probes focus with Tab, since that can indent the document. Prefer `ui_set_text` when GTK reports unreliable widget focus. In the measured GNOME Text Editor popover case, an explicit Ctrl+Tab restored focus before typing. Use that only after inspecting this app's state; Ctrl+Tab has different meanings in other applications. An unchanged document alone does not prove a failed typing attempt had no effect: menu shortcuts or another field may have received it. Inspect before any retry.

Screenshots/zoom return observation IDs when geometry is available. Pass `observation_id` with image-relative pointer coordinates and let the server transform them. Without that ID use desktop coordinates. Geometry changes invalidate frames, but content changes can also make targets stale: observe after scrolling, changing canvas zoom or opening a dialog. A failed required wait stops a batch; inspect any partial action before retrying.

A focused window can still be blocked by the GNOME overview or a modal dialog. Inspect the returned recovery image/state and dismiss or complete only the observed task-owned obstruction. Do not treat focus alone as permission to bypass an occlusion guard.

## Session ownership

For standalone window focus and arrangement, call `activate_window` or `window_layout` directly. `window_layout` cannot close windows; keep `window_manage` for closing. Batches and `desktop_exec` retain their own approval checks. The private launcher leaves existing desktops untouched. Keep one connection for a workflow; reconnecting normally allocates a fresh desktop. A future host that explicitly supplies a task ID can preserve that identity, but this registration does not promise it. If the selected task explicitly concerns an existing private session, address that named session through the existing CLI instead of assuming its windows moved. Save deliverables outside the private app home, close only this task's windows, and stop only its confirmed named desktop after work is complete. When cleanup or reconnect needs its name, use `desktop_health` or the existing `deskwright-headless list` command; this ownership check is conditional. On normal disconnect the launcher reclaims its empty connection-owned desktop. It preserves open windows or uncertain state for recovery; after an interrupted task, inspect the named session and save any remaining work before cleanup. Private app homes are retained for recovery. An unclean kill may leave a session running. The existing compositor count and memory caps remain enforced; never stop another task to make room.


Separate scripted tool timings from model task completion and native Windows/macOS comparisons.
