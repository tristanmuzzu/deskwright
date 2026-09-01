---
name: driving-the-desktop
description: How to drive a GNOME/Wayland desktop well with the wayland-computer-use tools, which tool to reach for first, how to prove an action landed, and how to work on the invisible second desktop instead of the user's screen. Use whenever a task means operating a native Linux application: launching an app, clicking or typing in a GUI, filling a dialog, testing a desktop flow, reading what is on screen, or recording something that moves.
---

# Driving the desktop

The expensive thing is not the work. It is the round trips. Every tool here is
built so one call can decide the next move without a screenshot in between.

## Reach for tools in this order

1. **`desktop_health` once, at the start.** One line says whether this desktop
   is usable and which one it is. Do not guess at capability; ask.
2. **The accessibility tree before pixels.** `ui_apps` → `ui_find` → `ui_press`
   presses the widget's own action: it cannot miss, cannot be defeated by the
   window moving, and needs no pointer. `ui_set_text` is the preferred way to
   put text in a field, no focus, no keyboard, and it reads the widget back to
   prove the write.
3. **`screen_map` when you need coordinates.** It returns windows top-of-stack
   first and every pressable widget of the focused app, each with a `ref: N`.
   Pass the ref straight to `ui_press(ref)` or `pointer_click(ref)`, identity
   is re-checked, so a stale ref fails loudly instead of clicking the wrong
   thing. Refs die at the next `screen_map`.
4. **`find_text` for Chrome, Electron and Qt.** Those toolkits expose almost
   nothing to `ui_find`. OCR gives screen coordinates in about 0.3 s with no
   image in the transcript, much cheaper than a screenshot you then have to
   look at.
5. **Pixels last.** `screenshot` when you genuinely need to see; `zoom` to look
   closer at one window, region or widget without scaling.

## Prove it landed

- `pointer_click` takes `expect_window`; a click that would land elsewhere is
  refused, and names the blocker so `on_occluded: "click_topmost"` can redirect
  in the same call.
- `ui_press` requires `expect_name`/`expect_role`. That is the identity check,
  not ceremony.
- Read the result. Acting tools report whether they landed; a click that
  changed nothing says so.
- `assert_state` turns "I think it worked" into a pass/fail with evidence, so
  a run can end itself.

## Never sleep, wait

`wait_for` handles `window_exists`, `window_gone`, `window_focused`,
`focus_changes`, `text_appears`, `widget_exists`, `clipboard_changed` and
`elapsed`. `region_changed` covers what those cannot express: a reply
arriving, a spinner finishing. A guessed `sleep` is either a wasted second or
a flaky step.

## Batch a known sequence

`do_steps` runs a sequence in one call, validated up front, with per-step
retry and one picture at the end (or at the step that failed). Use it the
moment you know the next three actions.

## Anything that moves needs a recording

A still cannot show motion. `screencast` records, then `frames` tiles it into
one contact-sheet PNG and reports a per-frame delta series with a jerk figure,
peak and mean cannot tell a smooth pan from a jolting one. Read the PNG; never
try to read the mp4.

## Work where the user is not looking

`WCU_SESSION=headless` (or `headless:<name>`) drives a private virtual-monitor
session the user never sees, so windows do not steal focus and a long
unattended run does not fight for the screen. Prefer it for anything the user
did not ask to watch. `headless:<name>` picks which desktop, so two agents can
work at once without watching each other's windows move.

## Two things to respect

- **The human halt switch is `Super+Ctrl+Escape`.** It is grabbed inside the
  compositor and cannot be pressed or cleared by injected input. If tools start
  refusing with a halt, a human stopped you on purpose, say so and stop.
- **Every acted call is journaled.** `journal` reads the trail back with
  arguments, outcome and verdict. Use it to reconstruct state after context
  loss instead of re-deriving it by clicking around.
