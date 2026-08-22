# Roadmap — every candidate improvement, categorized

Compiled 2026-08-23 from a competitive survey (Anthropic computer-use toolset,
OpenAI Codex computer use, Microsoft UFO², UI-TARS, Agent S3, browser-use,
agent-sh/computer-use-linux, OSWorld literature) plus a code audit of this
repo. Each item: **[effort]** S/M/L, and a risk note where a change could
affect the machine this currently runs on. Nothing here is committed-to;
this is the menu to test from.

Goal, stated once: become *the* Linux computer-use server for AI agents,
Claude Code first.

---

## P0 — Publish blockers (must happen before the repo goes public)

1. **Rename the personal namespace.** [S]
   `org.tristan.MigrationHelpers`, `migration-helpers@tristan.local`, and
   "Tristan" inside error strings (`mcp_server.py:115`, `mcp_server.py:174`,
   `desktop.py:104`). Pick a project name and a D-Bus name to match (e.g.
   `org.<project>.Shell`). Machine risk: extension UUID change needs one
   re-login; do it alongside an extension change that already needs one.
2. **Bundle the GNOME extension into this repo.** [S]
   It lives in `~/projects/gnome-migration-helpers` — a public repo that
   depends on a private second repo is dead on arrival. Ship it under
   `extension/` with an install script.
3. **Split `mcp_server.py` (3,705 lines).** [M]
   By domain: `capture.py`, `input.py`, `atspi.py`, `ocr.py`, `guards.py`,
   `steps.py`, `server.py`. Keep the tool surface identical; this is purely
   for contributors and testability.
4. **Installer + packaging.** [M]
   `pipx`/`uvx` installable; one `setup` command that enables
   `toolkit-accessibility`, installs the ydotoold system unit, installs and
   enables the extension, then prints what needs a re-login. Emit the
   `claude mcp add` / `.mcp.json` snippet.
5. **License, hygiene, metadata.** [S]
   Apache-2.0 or MIT; CONTRIBUTING.md; SECURITY.md; secrets/paths scan of
   git history before first push (history contains machine paths — decide
   squash-vs-keep).
6. **De-Tristanize the docs.** [S]
   README measurements are gold — keep them — but reframe machine-specific
   numbers ("this 1920x1080 display", the `de`-layout example) as *worked
   examples of a method*, not assumptions. Add a hardware/compositor
   support matrix.

## A — Portability (one machine → the Linux server)

7. **xdg-desktop-portal backend (the big one).** [L]
   Portal `RemoteDesktop` (libei input) + `ScreenCast`/`Screenshot`
   (PipeWire) as a second backend beside the extension. This is the
   cross-compositor "correct" Wayland path — it is what makes KDE and
   wlroots possible at all. Cost: one consent dialog per session (persist
   with `restore_token`). Keep the extension backend as the zero-dialog
   fast path on GNOME; select per capability at runtime via
   `desktop_health`-style probing. Machine risk: none if extension path
   stays default on GNOME.
8. **KDE Plasma support.** [M, after 7]
   KWin exposes window listing/activation via scripting D-Bus; kwin-mcp
   (41★) proves the input path. AT-SPI side is desktop-agnostic already.
9. **wlroots (Sway/Hyprland) support.** [M, after 7]
   `wlr-foreign-toplevel` for windows, portal for the rest. Hyprland users
   are exactly the audience that will star this repo.
10. **X11 fallback backend.** [S–M]
    XTEST + `xdotool`-class code is trivial next to what exists. Widens the
    audience to every legacy desktop and makes "works on any Linux" true.
    Low priority for the mission (X11 has 20 competitors) but cheap.
11. **HiDPI / fractional scaling / multi-monitor.** [M]
    Currently untested beyond one 1920x1080 panel. Coordinate space must be
    defined once (logical vs physical pixels) and every capture/input path
    must agree, per monitor. Needs: monitor enumeration in `screen_map`,
    scale-factor in `desktop_health`, tests on a virtual second monitor
    (mutter can create one headlessly).
12. **Keyboard layout hardening.** [S]
    The keysym path already beats ydotool here; make `layout_hazard`
    cover non-Latin layouts (Cyrillic, CJK input methods → route through
    AT-SPI `ui_set_text` and say so in the error).
13. **Flatpak/Snap app awareness.** [S]
    Sandboxed apps sometimes miss the AT-SPI bus or expose it late.
    Detect ("this app is a Flatpak without a11y access; here is the
    `flatpak override` to fix it") instead of returning an empty tree —
    the empty-tree-that-looks-broken failure mode, again.
14. **GNOME version matrix.** [S]
    Extension currently targets Shell 50. CI check of `metadata.json`
    versions + a documented support window (e.g. 47+).

## B — API robustness and polish (known rough edges first)

15. **`do_steps` validates lazily.** [S]
    A malformed step 4 currently fires steps 1–3 first. Validate the whole
    sequence up front; never start a run that cannot finish.
16. **`do_steps` `look: "region"` rejects `look_at` in both documented
    forms.** [S] Bug; fix plus a test that exercises every documented
    `look` shape.
17. **`sleep` cap (10 s) fights real workloads.** [S]
    Raise cap, or better: deprecate raw sleep in `do_steps` in favor of an
    inline `wait_for` step type — waiting on a condition is always better
    than a guessed duration (the codebase already believes this).
18. **Error taxonomy.** [M]
    Guards produce rich prose errors; give them stable machine-readable
    `code` fields (`focus_not_acquired`, `widget_moved`, `occluded`,
    `needs_relogin`, `locked`) so agent harnesses can branch on them
    instead of regexing prose.
19. **Schema tightening.** [S]
    Every tool schema reviewed for: enums where strings are magic, defaults
    stated, examples in descriptions. The description IS the UX for a
    model; token cost of descriptions is real — measure it (Anthropic's
    browser toolset costs ~6.6k tokens; know our number, publish it).
20. **Big-tree pagination.** [S]
    `ui_tree` on a monster app should page (cursor) rather than truncate.

## C — Perception (see better, cheaper)

21. **`zoom` tool.** [S]
    Anthropic's toolset added it for a reason: full-res crop of a named
    region/widget/window. We have crop internally — expose it as a
    first-class "look closer at X" that takes a widget path or region,
    no scaling ever.
22. **Set-of-Mark refs on `screen_map`.** [M]
    Number every actionable thing (`ref_7`), let `pointer_click`/`ui_press`
    accept `ref: 7`. browser-use's dominance is largely this pattern —
    it removes coordinate arithmetic from the model entirely and survives
    layout shifts between look and click. Refs must expire on tree change
    (the `expect_name` machinery already knows how).
23. **Tree/pixel cross-check on `ui_read_text`.** [M]
    Published finding (arXiv 2607.04334): models defer to a poisoned
    a11y/DOM value 41–79% of the time even when pixels disagree, and
    structure-pixel conflicts drive 91–100% task failure. Optional
    `verify: "ocr"` flag that OCRs the widget's bounds and reports
    agreement/disagreement instead of silently trusting the tree.
    Security feature; name it that in the docs.
24. **OCR quality ladder.** [S–M]
    Tesseract is fine but misses small/anti-aliased text. Optional
    backends (RapidOCR/PaddleOCR) behind the same `find_text` interface;
    keep tesseract the zero-extra-install default. Benchmark on the
    existing OCR test corpus before adopting anything.
25. **"What changed" as text.** [M]
    After an action, the before/after diff is already computed for
    hit/miss. Upgrade the miss/hit report to name *where* change happened
    (which window, which region, OCR of the changed cells) — often saves
    the follow-up screenshot entirely, which is the whole latency thesis.
26. **Screenshot lifecycle guidance for harnesses.** [S]
    Document the pruning pattern (Anthropic recommends batch-pruning old
    screenshots to preserve prompt caching); consider content-addressed
    shot names so a harness can dedupe.

## D — Action space (things it cannot do today)

27. **Clipboard tools.** [S]
    `clipboard_read` / `clipboard_write` (wl-clipboard or the portal).
    Huge leverage: "paste this 2 KB text" is one action instead of 2,000
    keystrokes, and reading a copy result is a verification primitive.
    Also the standard trick for CJK/emoji text entry where keysyms fail.
28. **Window management verbs.** [S–M]
    move/resize/close/minimize/maximize/tile, workspace switch, always-on-
    top. Mutter D-Bus (extension) already can; other backends via portal
    or compositor APIs. Needed for "arrange my screen" tasks and for
    keeping the agent's work out of the user's way.
29. **App launching.** [S]
    `launch_app(desktop_id, wait_for_window=true)` via `gio launch` +
    existing `wait_for`. Every real task starts with an app that is not
    running; today that is a shell command outside the protocol.
30. **`hold_key` / key-down/up, pointer down/up.** [S]
    Parity with Anthropic's toolset (games, gesture UIs, drag with
    modifier). RemoteDesktop session supports it; expose it.
31. **Drag verification.** [S]
    `pointer_drag` exists; give it the same before/after landed-or-not
    report clicks get (did the dragged thing move?).
32. **Scroll-into-view.** [M]
    `ui_press` on an off-screen widget: scroll its container until visible
    (AT-SPI `Component.ScrollTo` where implemented, wheel fallback), then
    act. Removes a whole class of "click missed because it was clipped".
33. **Text selection primitives.** [M]
    Select range in a text widget via AT-SPI `Text` interface (set
    selection offsets) with keyboard fallback. Windows-MCP lists this as
    a known gap — doing it is a differentiator.
34. **File-dialog helper.** [M]
    GTK file choosers are a notorious agent tarpit. A composite "in the
    open/save dialog, go to PATH and confirm" step (Ctrl+L + type + Enter)
    as a `do_steps` macro or dedicated tool.

## E — Verification, recovery, long tasks

35. **Retry policy in `do_steps`.** [M]
    Per-step `retry: {attempts, on: [codes]}` using the error taxonomy
    (#18). OSWorld literature: top performers differ on recovery loops
    more than on grounding. Keep it bounded and honest (report retries).
36. **Richer `wait_for` conditions.** [S–M]
    `text_appears(str, window)` (OCR or tree), `widget_exists(path)`,
    `clipboard_changed`. Every condition added kills N guessed sleeps.
37. **Assertions as a tool.** [S]
    `assert_state(window_focused=…, text_present=…)` returning pass/fail
    with evidence — lets a harness end a task with a *proof*, aligned
    with how this repo already thinks (tests prove, not claim).
38. **Action journal.** [M]
    Append-only JSONL of every act (tool, args, target window, hit/miss
    verdict, screenshot hashes). Enables: audit ("what did the agent do
    while I was away"), replay for bug reports, and later demo-to-macro
    recording (OpenAdapt's whole thesis). Cheap now, impossible to
    retrofit onto past sessions.
39. **Session checkpointing guidance.** [S]
    OSWorld 2.0 finding: long-horizon collapse is about context
    management, not grounding. Document the pattern (journal + screen_map
    snapshot as a resumable checkpoint) for harness authors.

## F — Safety and permissions (Codex/Claude Code are ahead here; close the gap)

40. **Per-app permission tiers.** [L]
    The single best idea worth stealing from Codex and Claude Code native
    computer use: config file mapping apps → `full | click_only |
    view_only | deny`, enforced server-side (guards already resolve the
    target window/app for every action, so the hook point exists).
    Defaults worth copying: browsers/password managers view-only,
    terminals click-only, unknown apps prompt-or-deny.
41. **Kill switch.** [M]
    Codex: global Esc, consumed so injection can't dismiss it. GNOME
    blocks client-side grabs — but the extension runs inside the shell
    and CAN own a keybinding. Panic key = extension flips a flag the
    server checks before every action; also `touch ~/.config/<name>/halt`
    as the no-GUI fallback. Must be un-dismissable by injected input.
42. **On-screen-content injection tripwire.** [M]
    Anthropic runs classifiers over screenshots for prompt injection.
    Local, cheap version: OCR-scan captured text for imperative-to-agent
    patterns ("ignore previous instructions", "run this command") and
    attach a warning block to the tool result rather than blocking.
    Honest scope: a tripwire, not a defense.
43. **Sensitive-surface redaction.** [M]
    Config: windows matching (password prompts, keyring dialogs, banking
    apps) are blacked out in screenshots and refused as action targets.
    Polkit/gcr prompt window classes are identifiable.
44. **Audit trail = #38.** Same journal, security framing: signed/append-
    only option.
45. **Session indicator honesty.** [S]
    The orange screen-share dot already appears during pointer sessions —
    document it as a feature (Codex ships a live preview window for the
    same reason). Consider extension-drawn "agent active" indicator for
    non-pointer actions too.
46. **Lock-screen policy config.** [S]
    `unlock-dialog` capture and locked-screen `ui_set_text` are currently
    always-on powers. Make both opt-in config for the public release;
    default conservative. (This machine: keep enabled.)

## G — Background / parallel operation (the dream feature)

47. **Headless second session.** [L, research first]
    Codex's killer feature: agents work while the user keeps the screen.
    Wayland equivalent: `gnome-remote-desktop` headless sessions (GNOME
    46+) or `mutter --headless` with virtual monitors — a second session
    on a virtual display, agent drives it via the same portal/extension
    stack, user watches (or ignores) via the RDP/PipeWire stream.
    Multi-seat is the alternative route. Hard, genuinely novel on Linux —
    nobody in the survey has it. Prototype = one spike: can a headless
    GNOME session + AT-SPI + portal input run a full `do_steps` flow?
    Machine risk: contained (separate session), but test on battery/RAM
    budget — 8 GB is the known constraint.
48. **Nested-compositor sandbox mode.** [M–L]
    Cheaper sibling of #47: run one target app inside a nested compositor
    (`mutter --nested` / cage) on a virtual output; agent owns that
    surface completely, main desktop untouched. Not every app cooperates;
    fine as an opt-in.
49. **Per-window capture streams.** [M]
    `screencast` of one window while the user works elsewhere is already
    possible via ScreenCast window sources; make interleaved use (agent
    watches window A, user uses window B) a documented, tested mode —
    input focus is the contested resource, so pair it with `ui_set_text`
    (focus-free) actions and #28 window placement.

## H — Ecosystem and distribution (Claude Code first)

50. **Ship as a Claude Code plugin.** [M]
    Plugin = MCP server + a skill that teaches the tool-preference ladder
    (the README's ordering table, rewritten as agent instructions) +
    optional hooks (e.g. block `rm`-class actions while an agent session
    drives the GUI). One `claude plugin install` beats a manual MCP add.
51. **MCP registry + mcpb bundle.** [S–M]
    List in the MCP registry; build an `.mcpb` for double-click install in
    Claude Desktop. Check the Linux Desktop beta's MCP support and be
    ready the day its computer use stays disabled (that gap is the
    market).
52. **Works-with-any-client examples.** [S]
    OpenAI Responses harness example, LangChain/agno snippet, plain
    Python MCP client script. Windows-MCP markets "works with text-only
    LLMs" — our `ui_*`/`find_text` path genuinely does; say so.
53. **README rewrite for the public.** [M]
    Current README is a brilliant lab notebook for insiders. Public repo
    needs: 30-second pitch, GIF demo, install, support matrix, THEN the
    deep material (keep all of it — the measurements are the moat).
54. **Publish the research.** [M]
    The latency table, scale-kills-OCR, contrast-cell hit/miss threshold,
    Chrome a11y-flag measurements — a blog post / `docs/measurements.md`.
    This is original data nobody else has published; it will carry the
    launch (HN title writes itself: "Why AI agents can't use Linux, and
    what it took to fix it").
55. **Demo assets.** [S]
    `screencast` + `frames` can record the agent driving a real task —
    the tooling to make its own demo GIF already exists. Dogfood it.

## I — Testing, CI, benchmarks

56. **Portable test suite.** [M]
    Tests currently assume this desktop. Ship a witness app (tiny GTK4
    window that reports what it receives — `test_pointer.py` already has
    the pattern) and make the suite run on any GNOME session, then in CI.
57. **CI on a virtual GNOME session.** [L]
    GitHub Actions runner + headless GNOME (podman, `mutter --headless` or
    GNOME OS image) exercising the real stack per PR. Hard; #47's spike
    feeds this directly. Until then: unit-test the pure logic (parsing,
    guards, deltas) which the module split (#3) unlocks.
58. **OSWorld-subset self-benchmark.** [M]
    Port ~20 OSWorld-style Linux tasks (LibreOffice, GIMP, file manager)
    with programmatic checkers; publish success rate + median actions +
    wall-clock per model. First-ever Wayland datapoint in that literature;
    doubles as a regression suite.
59. **Latency regression tracking.** [S]
    The round-trip table is the product thesis; re-measure per release
    (script exists in spirit in the transcripts analysis) and publish
    trend.

## J — Performance

60. **Persistent capture session.** [M]
    If screenshot cadence is high, a standing ScreenCast/PipeWire stream
    beats one-shot captures (Screenshot each time = new capture). Measure
    first — 0.23 s/capture may already be fine; idle stream costs CPU on
    an 8 GB/battery machine. Auto-open on burst, auto-close idle (the
    25 s pointer-session pattern, reused).
61. **Tree caching with invalidation.** [S–M]
    `ui_find` depth-30 walks are repeated per call; cache per app keyed on
    AT-SPI `children-changed` events, invalidate on any event. Measure
    before building (walk cost unknown-cheap).
62. **Token budgets, stated.** [S]
    Per-tool typical token cost in each tool description (a `screen_map`
    is ~N tokens, a window shot ~1300…). Models plan better when cost is
    visible; harness authors quote it.

## K — Positioning (decisions, not code)

63. **Name.** Needs one that isn't `wayland-computer-use` (descriptive but
    unownable) — pick before the extension rename (#1) so the D-Bus name
    matches. No strong proposal yet; decide with Tristan.
64. **License Apache-2.0** (patent grant matters for automation tooling
    aimed at corporate adopters) unless MIT preferred for simplicity.
65. **Scope statement.** README states what it will NOT do: no CAPTCHA
    solving, no credential typing (pairs with #40/#43 defaults), no
    detection evasion. Same guardrails the big vendors ship; costs
    nothing, buys trust.
66. **Open source, free.** Survey verdict stands: the paid market is too
    thin and agent-sh gives the same recipe away at 422★. The prize is
    becoming the default („the Linux computer-use server"), which only
    adoption buys.

---

## Suggested testing order (everything reversible)

1. Bugs and small parity first: #15 #16 #17 #21 #27 #29 #30 (days, zero risk).
2. P0 mechanical pass: #1–#6 (one re-login).
3. The two moats, spiked in parallel: portal backend (#7) and headless
   session (#47) — each starts as a one-file experiment, kept only if the
   spike proves out.
4. Set-of-Mark refs (#22) + error taxonomy (#18) + retry (#35) as one
   coherent "harness ergonomics" release.
5. Safety tier (#40 #41 #43) before any public announcement.
6. Benchmarks and launch material (#53 #54 #58) last, with real numbers.
