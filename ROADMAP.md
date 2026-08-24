# Roadmap — autonomy-first, categorized and ordered

## Status (2026-08-23, end of the two-session build)

**Shipped and live** (33 tools, all suites green on GNOME 50 Wayland):
module split; machine-readable error codes; `do_steps` up-front
validation + per-step retry; `wait_for` with `text_appears` /
`widget_exists` / `clipboard_changed`; `assert_state`; `zoom`;
clipboard read/write; `hold_key`; drag verification; `launch_app`;
scroll-into-view; document-identity pinning (+ the widget-focus-void
fix); "what changed" as text; `window_manage`; the bundled
`wcu@wayland-computer-use` extension (window verbs, clipboard, halt
switch, activity indicator) with bus preference + fallback; the halt
gate; Set-of-Mark refs; the action journal; the injection tripwire;
the `wcu-setup` installer + `pyproject.toml`; public README / LICENSE /
CONTRIBUTING / SECURITY; and the Claude Code auto-approval doc.

**Shipped 2026-08-24: the headless second session (#19).** `wcu-headless`
start/stop/status/env + `WCU_SESSION=headless` server pinning; input over
the private session's own mutter RemoteDesktop (the spike's open question —
it follows the bus like everything else); private `XDG_RUNTIME_DIR` after
a shared one broke the primary session's a11y bus; `gio launch` replaced by
direct Exec spawn on headless (D-Bus activation loses the window there);
ydotool refused with `wrong_session`. Proven live: launch →
compositor-confirmed click → do_steps type → AT-SPI read-back →
screenshot, all on the virtual monitor. README § "The headless second
session".

**Proven feasible, not yet built** (see `docs/spikes.md`): the portal/libei
backend — one wiring task left (absolute-motion ScreenCast link).

**Deliberately session 3+**: KDE/wlroots/X11 backends (follow the portal
backend), HiDPI/multi-monitor, CI on a virtual GNOME session, the
OSWorld-subset benchmark, MCP-registry/plugin publishing, the opt-in
restrictive-policy config. None block a publishable core.

---

Compiled 2026-08-23 from a competitive survey (Anthropic computer-use toolset,
OpenAI Codex computer use, Microsoft UFO², UI-TARS, Agent S3, browser-use,
agent-sh/computer-use-linux, OSWorld literature) plus a code audit of this
repo. Revised same day after a design decision (below). Each item:
**[effort]** S/M/L, and a machine-risk note where relevant.

## Design philosophy (decided 2026-08-23)

**The agent is trusted; the tooling's job is to make it capable and
self-sufficient, not to fence it in.** Concretely:

- The server never blocks a capability the platform allows. No view-only
  app tiers by default, no redacted windows by default, no artificial
  browser restrictions. Restrictive policy exists only as *opt-in config*
  for deployments that want it (public users get the knob; this machine
  keeps it off).
- Safety budget goes to exactly three things: (1) a kill switch a human
  can always reach, (2) irreversibility backstops so one bad action
  cannot destroy a machine or an account, (3) honest evidence — the agent
  and the user can always see what actually happened. Everything else is
  the agent's judgment.
- Every round trip the agent does NOT need — every question it does not
  have to ask, every screenshot it does not have to request, every retry
  it can decide alone — is the product. Autonomy is a latency and
  evidence problem, not a permission problem.

Goal, stated once: the Linux computer-use server for AI agents — Claude
Code first — capable of long unattended runs.

---

## Tier 1 — Autonomy core: the agent finishes tasks without help

The highest-value work in the file. Each item removes a class of
"agent got stuck / guessed / asked a human".

1. **Machine-readable error codes.** [M]
   `focus_not_acquired`, `widget_moved`, `occluded`, `needs_relogin`,
   `locked`, `app_not_on_bus`… Guards already produce rich prose; codes
   let the agent (and `do_steps` retry, next item) branch and self-recover
   instead of re-reading prose and guessing.
2. **Per-step retry policy in `do_steps`.** [M]
   `retry: {attempts, on: [codes]}`. OSWorld literature: top performers
   differ on recovery loops more than grounding. Bounded, honest
   (retries reported in the result).
3. **Richer `wait_for` conditions.** [S–M]
   `text_appears(str, window)` (OCR or tree), `widget_exists(path)`,
   `clipboard_changed`. Every added condition kills N guessed sleeps and
   N "let me take another screenshot to check" round trips.
4. **`do_steps` validates the whole sequence up front.** [S]
   Today a malformed step 4 fires steps 1–3 first — an autonomous run
   that half-executes is the worst outcome. Never start what cannot
   finish. (Known bug, same family: `look: "region"` rejects `look_at`
   in both documented forms — fix together.)
5. **Replace the 10 s `sleep` cap with an inline `wait_for` step type.** [S]
   Waiting on a condition beats a guessed duration; the codebase already
   believes this everywhere else.
6. **"What changed" as text after every action.** [M]
   The before/after diff is already computed for hit/miss. Upgrade the
   report to name *where* change happened (which window, which region,
   OCR of changed cells). Often eliminates the follow-up screenshot —
   the single biggest per-step latency saving available.
7. **`assert_state` tool.** [S]
   `assert_state(window_focused=…, text_present=…, widget_exists=…)`
   returning pass/fail with evidence. Lets the agent *prove* completion
   to itself and end the run — self-verification is what separates
   autonomous from babysat.
8. **Scroll-into-view before acting.** [M]
   `ui_press` on a clipped/off-screen widget: scroll its container until
   visible (AT-SPI `Component.ScrollTo` where implemented, wheel
   fallback), then act. Removes a whole stuck-class without any human.
9. **Session checkpointing pattern.** [S]
   OSWorld 2.0 finding: long-horizon collapse is context management, not
   grounding. Document journal + `screen_map` snapshot as a resumable
   checkpoint for harness authors; long unattended runs depend on it.

## Tier 2 — Capability gaps: things it simply cannot do today

Every gap here is a task the agent must currently hand back to a human.

10. **Clipboard read/write.** [S]
    wl-clipboard or portal. Paste beats 2,000 keystrokes; clipboard read
    is a verification primitive; standard trick for CJK/emoji where
    keysyms fail.
11. **`launch_app(desktop_id, wait_for_window=true)`.** [S]
    Every real task starts with an app that is not running; today that
    is a shell command outside the protocol.
12. **Window management verbs.** [S–M]
    move/resize/close/minimize/maximize/tile, workspace switch. Mutter
    D-Bus (extension) already can. Needed for "arrange my screen" and
    for the agent keeping its work out of the user's way.
13. **`zoom` tool.** [S]
    Full-res crop by widget path / region / window — "look closer at X"
    as a first-class verb, never scaled. Anthropic added one for a
    reason.
14. **Set-of-Mark refs.** [M]
    Number every actionable in `screen_map` (`ref_7`); `pointer_click` /
    `ui_press` accept `ref: 7`. Removes coordinate arithmetic from the
    model and survives layout shift between look and act. Refs expire on
    tree change (the `expect_name` machinery already knows how).
    browser-use's dominance is largely this pattern.
15. **hold_key / key down-up / pointer down-up.** [S]
    Toolset parity (games, gesture UIs, modifier-drags). RemoteDesktop
    session supports it; expose it.
16. **Drag verification.** [S]
    Clicks verify, drags don't. Same before/after landed-or-not report.
17. **Text-selection primitives.** [M]
    Select range via AT-SPI `Text` interface, keyboard fallback.
    Windows-MCP lists this as a known gap — doing it is a differentiator.
18. **File-dialog helper.** [M]
    GTK file choosers are an agent tarpit. Composite "in the dialog, go
    to PATH and confirm" (Ctrl+L + type + Enter) as a macro or tool.

## Tier 3 — Unattended operation: runs while nobody watches

The dream tier. An autonomous agent that needs the user's screen is only
half autonomous.

19. **Headless second session (the big spike).** [L]
    `gnome-remote-desktop` headless sessions (GNOME 46+) or
    `mutter --headless` + virtual monitors: agent drives a second session
    on a virtual display via the same stack; user keeps their screen and
    can peek via the stream. Codex's killer feature (macOS
    background-parallel); nobody on Linux has it. Spike: can a headless
    session + AT-SPI + input run one full `do_steps` flow? Machine risk:
    contained (separate session) but measure RAM — 8 GB budget.
20. **Auto-approval story for Claude Code.** [S–M]
    An autonomous server whose every call throws a permission prompt is
    not autonomous. Ship a recommended `settings.json` allowlist
    (all `mcp__<name>__*` tools pre-approved), documented as THE intended
    configuration, plus a note on which tools a cautious deployment
    might leave prompted. This is cheap and moves autonomy more than any
    single feature.
21. **Per-window capture streams for coexistence.** [M]
    Agent watches window A while the user works in window B. ScreenCast
    window sources make it possible; pair with focus-free `ui_set_text`
    and window placement (#12). Documented, tested mode.
22. **Nested-compositor sandbox mode.** [M–L]
    One target app inside `mutter --nested`/cage on a virtual output;
    agent owns that surface completely, desktop untouched. Opt-in;
    not every app cooperates. Cheaper sibling of #19.
23. **Unattended resilience.** [M]
    Survive the things that end long runs: portal `restore_token`
    persistence (no re-consent), screen-lock transitions (a11y path
    already survives; make every tool degrade gracefully and say so),
    `needs_relogin` detection with a precise instruction in the error.
24. **Scheduled/headless-friendly browser path.** [S]
    Document the `--force-renderer-accessibility --user-data-dir` recipe
    (already proven end-to-end in the README) as the sanctioned route for
    cron/headless runs where claude-in-chrome is unavailable.
25. **Action journal.** [M]
    Append-only JSONL: tool, args, target window, hit/miss verdict,
    screenshot hashes. For autonomy this is *evidence*, not surveillance:
    the user reviews what happened after the fact instead of supervising
    during; the agent can re-read its own trail after a compaction;
    bug reports become replayable. Impossible to retrofit later.

## Tier 4 — Publish blockers (mechanical, before the repo goes public)

26. **Rename the personal namespace.** [S]
    `org.tristan.MigrationHelpers`, `migration-helpers@tristan.local`,
    "Tristan" in error strings (`mcp_server.py:115`, `:174`,
    `desktop.py:104`). One re-login; batch with any extension change.
27. **Bundle the GNOME extension into this repo** with an installer. [S]
28. **Split `mcp_server.py` (3,705 lines)** by domain: capture / input /
    atspi / ocr / guards / steps / server. Tool surface unchanged. [M]
29. **Installer + packaging.** [M]
    `uvx`-able; one `setup` command (a11y flag, ydotoold unit, extension,
    prints what needs re-login); emits `claude mcp add` / `.mcp.json`
    snippet — including the Tier-3 #20 allowlist.
30. **License Apache-2.0, CONTRIBUTING, SECURITY, history scan.** [S]
31. **De-Tristanize docs.** [S]
    Keep every measurement; reframe machine-specific numbers as worked
    examples of a method. Add support matrix.

## Tier 5 — Portability (one machine → every Linux desktop)

32. **xdg-desktop-portal backend.** [L]
    Portal `RemoteDesktop` (libei input) + `ScreenCast`/`Screenshot`
    (PipeWire) beside the extension backend. The cross-compositor correct
    path — what makes KDE and wlroots possible. One consent dialog per
    session, persisted via `restore_token` (#23). Extension stays the
    zero-dialog fast path on GNOME; runtime capability probing picks.
33. **KDE Plasma support.** [M, after 32] KWin scripting D-Bus for
    windows; kwin-mcp (41★) proves the input path.
34. **wlroots (Sway/Hyprland).** [M, after 32] `wlr-foreign-toplevel` for
    windows; that crowd is exactly who stars this repo.
35. **X11 fallback backend.** [S–M] XTEST is trivial next to what exists;
    makes "works on any Linux" literally true. Low mission priority
    (X11 has 20 competitors), cheap.
36. **HiDPI / fractional scaling / multi-monitor.** [M]
    Define the coordinate space once (logical vs physical, per monitor);
    monitor enumeration in `screen_map`; scale factor in
    `desktop_health`; test on a mutter virtual monitor.
37. **Keyboard layout hardening.** [S] Non-Latin/IME → route to
    `ui_set_text`, and say so in the error.
38. **Flatpak/Snap a11y detection.** [S] Detect the sandboxed-app
    empty-tree case and answer with the `flatpak override` fix instead of
    an empty tree.
39. **GNOME version matrix.** [S] Documented support window; CI check on
    `metadata.json`.

## Tier 6 — Minimal safety backstop

Small by design (see philosophy). Three jobs: human can always stop it,
one action can't destroy the machine, everything leaves evidence.

40. **Kill switch.** [M]
    Extension owns a shell-level panic keybinding (client grabs are
    blocked, the extension isn't) that flips a flag checked before every
    action; injected input cannot dismiss it. `halt` file as the no-GUI
    fallback. Costs the agent nothing until pressed.
41. **Irreversibility backstop.** [S–M]
    Not app tiers, not confirmations — a narrow tripwire for the tiny set
    of genuinely unrecoverable patterns (the machine-deletion class).
    Config-listed, default list short. Warn-and-proceed or require a
    second identical call ("press again to confirm") rather than asking
    a human.
42. **Injection tripwire (protects the agent's autonomy).** [M]
    OCR-scan captured text for imperative-to-agent patterns ("ignore
    previous instructions", "run this command") and attach a warning
    block to the result. A hijacked agent is the fastest way to lose the
    trust that autonomy runs on. Warn, never block.
43. **Evidence = #25 journal.** Optional signed/append-only mode.
44. **Session indicator honesty.** [S]
    Orange screen-share dot already appears during pointer sessions —
    document as a feature; optional extension-drawn "agent active" badge.
    Visibility instead of restriction.
45. **Opt-in policy config for cautious deployments.** [M, low priority]
    The per-app tier / redaction machinery (view-only browsers, blacked
    password managers) as a config file that DEFAULTS TO OFF. Public
    users who want fences get them; this machine and the default install
    run open. Ship late; it must never complicate the open path.

## Tier 7 — Ecosystem and distribution (Claude Code first)

46. **Ship as a Claude Code plugin.** [M]
    MCP server + a skill teaching the tool-preference ladder (the
    README's ordering table as agent instructions) + the #20 allowlist
    in the plugin's recommended settings. One `claude plugin install`.
47. **MCP registry + `.mcpb` bundle.** [S–M]
    Registry listing; double-click install for Claude Desktop. The Linux
    Desktop beta ships with computer use disabled — that gap is the
    market; be installable the day someone hits it.
48. **Works-with-any-client examples.** [S]
    OpenAI Responses harness, plain Python MCP client. Market
    "works with text-only LLMs" — the `ui_*`/`find_text` path genuinely
    does.
49. **README rewrite for the public.** [M]
    30-second pitch, GIF demo, install, support matrix — then the full
    lab notebook (keep all of it; the measurements are the moat).
50. **Publish the research.** [M]
    Latency table, scale-kills-OCR, contrast-cell hit/miss thresholds,
    Chrome a11y-flag measurements. Original data nobody else has
    published; carries the launch.
51. **Dogfood demo assets.** [S]
    `screencast` + `frames` record the agent driving a real task; the
    tool makes its own demo GIF.

## Tier 8 — Testing, benchmarks, performance

52. **Portable test suite** via a shipped witness app (the
    `test_pointer.py` pattern, generalized). [M]
53. **CI on a virtual GNOME session** (podman + `mutter --headless` or
    GNOME OS image). Hard; feeds directly off the #19 spike. Until then:
    unit-test the pure logic the #28 split unlocks. [L]
54. **OSWorld-subset self-benchmark.** [M]
    ~20 OSWorld-style Linux tasks with programmatic checkers; publish
    success rate, median actions, wall-clock per model. First Wayland
    datapoint in that literature; doubles as the regression suite and as
    the autonomy scoreboard (Tier 1/2 items should visibly move it).
55. **Latency regression tracking per release.** [S] The round-trip table
    is the product thesis; re-measure and publish the trend.
56. **Persistent ScreenCast stream for burst capture.** [M] Measure
    first; auto-open on burst, auto-close idle (reuse the 25 s
    pointer-session pattern). 8 GB machine: watch idle cost.
57. **AT-SPI tree caching** keyed on `children-changed` events. [S–M]
    Measure before building.
58. **Token budgets stated per tool description.** [S] Models plan better
    when cost is visible; measure our whole-toolset description cost and
    publish it.
59. **Schema polish.** [S] Enums over magic strings, defaults stated,
    examples in descriptions; pagination for `ui_tree`.

## Tier 9 — Positioning decisions (not code)

60. **Name.** `wayland-computer-use` is descriptive but unownable; decide
    before #26 so the D-Bus name matches. Tristan's call.
61. **Scope statement, autonomy-flavored.** README states the project's
    design philosophy openly (agent-trusted, evidence over fences,
    opt-in policy for cautious deployments) and what it deliberately
    does not do as a *project* (no CAPTCHA-solving features, no
    detection evasion). Honest positioning beats vendor-style fencing
    and buys trust with exactly the audience this is for.
62. **Open source, free.** Survey verdict stands: paid market too thin,
    agent-sh gives the recipe away at 422★. The prize is being the
    default, and only adoption buys that.

---

## Removed / demoted from the first draft (2026-08-23 reframe)

- **Per-app permission tiers as a core feature** → demoted to #45,
  opt-in config, default off. The agent is trusted.
- **Sensitive-surface redaction by default** → folded into #45, default
  off.
- **Lock-screen powers opt-in-conservative** → inverted: they stay on;
  they are an unattended-operation feature (#23), not a risk to manage.
- **Vendor-style "watch mode" ideas** → never adopted. Evidence after
  the fact (journal, indicator) replaces supervision during.

## Build plan — two sessions, subagent-parallel where safe

Written 2026-08-23 for execution starting the next session. Ordering is
driven by four hard constraints, stated once so every phase makes sense:

- **C1 — The desktop is a serialized test resource.** Any number of
  subagents can write code in parallel (git worktrees), but only ONE
  thing at a time may drive the real desktop. All live verification runs
  through the main session via `tests/mcpdrv.py` (the server Claude Code
  itself holds is stale-on-disk; mcpdrv speaks to a fresh one).
- **C2 — Extension changes need a re-login, and only one is available
  per session boundary.** Every extension-touching item must land in a
  single batch at the END of session 1; Tristan logs out/in before
  session 2; session 2 opens by verifying the batch.
- **C3 — The module split must precede parallel work.** Subagents
  editing one 3,705-line file collide; after the split they own separate
  files.
- **C4 — The machine must remain working at every commit.** Full
  `--self-test` + test suite after every phase; every phase is a
  separate commit; anything that fails verification is reverted, not
  parked half-done.

### Session 1 — foundation, autonomy core, extension batch

**Phase 0 — baseline (main thread, ~minutes).**
Run the full existing suite + `--self-test`, record results as the
regression baseline. Nothing is attempted on a broken baseline.

**Phase 1 — module split, #28 (main thread, serial).**
`mcp_server.py` → `capture.py` / `input.py` / `atspi.py` / `ocr.py` /
`guards.py` / `steps.py` / `server.py`. Pure mechanical move, zero
behavior change, tool surface identical. Full suite must pass bit-for-
bit. Commit. This unlocks every parallel phase after it.

**Phase 2 — error-code pass, #1 (main thread, serial).**
Cross-cutting by nature (touches every guard), so it cannot be
parallelized and must precede the items that branch on codes (#2, #41).
Codes added alongside existing prose, nothing removed. Commit.

**Phase 3 — parallel subagent wave (worktree isolation, code only).**
Independent files after the split; none may touch the live desktop —
they write code + unit tests, main thread verifies live afterward:
- Agent A (`steps.py`): #4 up-front `do_steps` validation + the
  `look: "region"`/`look_at` bug, #5 `wait_for` step type replacing the
  sleep cap.
- Agent B (`input.py`): #10 clipboard tools, #15 hold_key / key and
  pointer down-up, #16 drag verification.
- Agent C (`capture.py`/`ocr.py`): #13 `zoom` tool, #6 "what changed"
  as text in action results.
- Agent D (`atspi.py`): #11 `launch_app`, #8 scroll-into-view.
- Agent E (docs, no code): #20 recommended auto-approval
  `settings.json` allowlist + documentation page.
Merge one branch at a time; after each merge the MAIN thread runs live
verification through mcpdrv (C1). Commit per merge.

**Phase 4 — recovery spine, #2 #3 #7 (main thread, sequential).**
Retry policy (needs #1's codes) → richer `wait_for` conditions →
`assert_state`. Live-verified as one flow: a deliberately flaky
`do_steps` run that recovers by itself and proves its own completion.
This is the release that changes how runs feel; verify it end to end.

**Phase 5 — the extension batch (main thread, LAST in session 1, C2).**
All shell-side work in one go, in the bundled-extension layout (#27):
- #26 namespace rename (D-Bus name + UUID + error strings) — name
  decision (#60) needed here; if none exists yet, pick the working name
  and record it, renaming again is cheap before publish.
- #12 window-management verbs (extension D-Bus methods).
- #40 kill-switch keybinding + flag.
- #44 "agent active" indicator.
- Plus the already-written-but-never-loaded methods from the last round
  (`Pointer`, `WindowAt`, `ScreenshotArea`, `ScreenshotWindow`).
Server-side counterparts written and unit-tested, live verification
IMPOSSIBLE until re-login — explicitly deferred to session 2. Session 1
ends with: "log out and back in before the next session."

### Session 2 — verify, moats, publish mechanics

**Phase 6 — post-relogin verification (main thread, first thing).**
`desktop_health` must list every new extension method; then live-verify
the whole Phase 5 batch + a full regression run. Anything broken gets
fixed before new work starts.

**Phase 7 — the two moat spikes (timeboxed, main thread one at a time).**
Both are experiments kept only if they prove; both need the real
machine and possibly consent dialogs, so they cannot be subagented:
- #19 headless second session: can `gnome-remote-desktop` headless /
  `mutter --headless` + virtual monitor run one full `do_steps` flow?
  Measure RAM against the 8 GB budget. Timebox ~1h; a clean
  yes/no/blocked-at-layer-X finding is the deliverable either way.
- #32 portal backend: one-file spike — portal `RemoteDesktop` +
  `ScreenCast` session, one click + one capture through it,
  `restore_token` persistence (#23) checked. Same timebox, same
  finding-shaped deliverable.
Whichever proves out becomes the headline roadmap item for session 3+;
neither blocks publishing.

**Phase 8 — parallel subagent wave 2 (worktree, code only).**
- Agent F: #14 Set-of-Mark refs in `screen_map` + ref-accepting click
  path (`atspi.py`/`capture.py`).
- Agent G: #41 irreversibility backstop + #42 injection tripwire
  (`guards.py`/`ocr.py`).
- Agent H: #25 action journal (new `journal.py`, hooks into `server.py`).
- Agent I: #29 installer/`setup` command + packaging skeleton.
- Agent J: #49 public README restructure + #31 de-Tristanized docs +
  #30 license/CONTRIBUTING/SECURITY.
Same merge discipline: one at a time, live verification between merges.

**Phase 9 — wrap (main thread).**
#52 portable-witness test pass over everything new; re-measure the
latency table (#55) and update the README numbers; final full suite +
`--self-test`; update this file's checkboxes; commit, push.

### Explicitly NOT in these two sessions

KDE/wlroots/X11 backends (#33–#35, need the portal spike's outcome
first), HiDPI/multi-monitor (#36, needs hardware or virtual-monitor
work), CI-on-virtual-GNOME (#53), OSWorld benchmark (#54), MCP
registry/plugin shipping (#46–#47), opt-in policy config (#45). All
deliberately session 3+; none block a working, publishable core.

### Standing rules for the run

- Branch-per-agent, merge serially, live-verify per merge (C1, C4).
- Commit after every verified phase; push at session end.
- A spike that fails is a finding, not a failure — write it down
  (which layer said no, what it would cost) and move on.
- Scope discipline: anything discovered mid-run that is not on this
  plan goes to FINDINGS.md or this file, not into the working tree.
