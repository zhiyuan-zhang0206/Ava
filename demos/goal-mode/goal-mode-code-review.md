# Goal Mode — Demos & Test Record

**What this shows**: The evaluator-optimizer pattern — a supervisor agent watches
a worker agent across turns, judging its output against a goal until the goal is
met, telling it exactly what is missing after every round.

## Testing status — honest

**Completion-judged goal mode is lightly tested — the reject loop now has one
real exercise.** As of 2026-08-13 there are four recorded real runs (detailed
below). The first three — small builds — all completed on the first judgment
round with nothing to reject. The fourth — a multi-hour ledger app — finally
drove the loop for real: **two genuine rejections** (round 1: four missing
files, category [omitted]; round 2: a boot-crash API bug + a skipped verification,
categories [misunderstood]+[slacked]), each with path/line/repro evidence, then a verified
completion. That is the core multi-round path working once — not a measured
capability. A second mechanism bug was found and fixed mid-campaign (the
watcher redis-py-8 timeout, below). Treat goal mode as: watcher loop verified,
reject loop exercised once, judgment quality good in the one exercise — and
still not sufficiently tested for a claim stronger than that.

## Demo 1 — Code review (spec)

```
Use goal mode to help me review a piece of code.

First, load the goal skill: ava.help(ava.skills.ava_goal)

Then:
1. Spawn a worker agent and have it implement a simple TODO list CLI (Python)
2. Start the goal watcher to monitor this worker
3. Whenever the worker is idle, check its code quality:
   - Does it have type hints?
   - Does it have docstrings?
   - Does it have error handling?
   - Is the code structure clear?
4. If it doesn't meet the standards, tell the worker what's missing
5. Once it meets the standards, render the final code to HTML and display it with ava.ui.serve

The worker needs at least 2-3 iterations to reach the quality standards.
```

Expected flow: spawn worker → launch watcher → worker implements a first cut →
watcher wakes the supervisor → "missing error handling, no docstrings" → round 2 →
"args parsing is fragile" → round 3 → quality bar met → final code presented via
HTML served with `ava.ui.serve`.


## Real runs (2026-08-13)

All runs used the same topology: supervisor spawns a worker (deepseek-v4-flash)
with no prompt, arms the one-shot idle watcher, then sends the goal. The
supervisor judges the **artifact**, never the worker's claim: it re-runs every
check, reads every file, and drives the UI in an independent DOM smoke harness
(jsdom) where a real browser was unavailable.

### Run 1 — 2048 game (`demos/goal-mode/2048/`)

Community-standard goal-mode demo shape (build a game from a brief — the
canonical Codex `/goal` use case), small and crisply specified. Verifiable
criteria: syntax checks, plain-Node logic tests, code review, exactly four
files. **Result: goal met on judgment round 1.** Evidence: `node --check` clean,
26/26 logic tests re-run green, full code review, worktree clean.

### Run 2 — Snake arcade package (`demos/goal-mode/snake/`)

Harder: 14 numbered requirements with planted edge cases (180° reversal
ignored, tail-vacate legality, full-board win, speed cap, top-5 persistence,
auto-pause on tab hide, zero external assets). **Result: goal met on judgment
round 1.** Evidence: 1,086 assertions re-run green, full code review, zero
external refs, plus an independent 34-check DOM smoke (pause via P/Space/button,
visibilitychange auto-pause, d-pad, a real wall-death game-over flow,
localStorage high-score lists, restart).

### Run 3 — Weekly planner (`demos/goal-mode/weekly-planner/`)

Deliberately user-shaped: a multi-file app whose requirements are embedded in
prose ("my friend is picky..."), never enumerated — 15+ non-explicit
requirements (120-char truncation in stored data, empty-title inline error,
keyboard move, screen-reader announcements, reduced motion, ≤20 tasks per day +
"show more", `Mon 8/13` date format, no ISO, delete confirm, 375px layout,
file:// only, no console errors). **Result: goal met on judgment round 1.**
Evidence: 20/20 tests re-run green, full code review of all five files, and an
independent 31-check DOM smoke (add/trim/dupe/newest-first, storage truncation,
select + arrow-key move with focus follow, confirm-delete + Esc, 20/day window +
show-more count, persistence across reload, zero uncaught errors).

### Run 4 — Ledger, a long task with real rejections (`demos/goal-mode/ledger/`)

The community's long-horizon shape: a multi-view personal finance tracker
(dashboard / transactions / budgets / charts), ~2,600 lines total, with subtle
objectively-verifiable requirements (integer cents, RFC-4180 CSV round-trip with
atomic import, category-id reassignment, 200ms debounced search, budget clamp,
canvas chart, persistence + corrupt recovery, a11y, 375px, zero console errors).
Deliberately too large for one turn, so rounds are physical, not cosmetic.

| Round | Verdict | Defects (category → evidence) |
|---|---|---|
| 1 | **reject** | [omitted] ×4 — index.html/style.css/app.js absent, no DOM verification possible (`ls` shows only store.js+test.js). store.js+test.js passed review and were declared do-not-rework. |
| 2 | **reject** | [misunderstood] ×1 — `app.js` calls `store.sortedEntries()` but the store API (store.js:592-608) does not expose it → `TypeError` at boot, whole UI dead (jsdom repro, app.js:125). [slacked] ×1 — shipped without running the app once; the worker's own report admitted no DOM verification, and one boot would have caught defect 1. |
| 3 | **goal met** | Worker fixed both, caught a Chrome-only debounce bug (`setTimeout` dereferenced → Illegal invocation) via a real-browser run, and added its own 24-check jsdom harness. Supervisor re-verified independently: 53/53 tests, worker's harness re-run 24/24, a separate 30-check supervisor smoke (fresh assertions) green with zero errors. |

What this run establishes: on a long task the reject loop engages for real and
with teeth — the round-2 rejection forced the worker to add a verification layer
it had skipped, and the final quality (double-layer DOM verification) is visibly
higher than any single-round run's. The evidence-based rejection format (defect
category + path:line + repro) worked: the worker fixed exactly the named defects
with no thrash. One run of the reject loop, so: working as designed, still
unmeasured at scale.

### What the three single-round runs say

Task difficulty (hidden requirements, prose briefs) did **not** produce the
expected multi-round dynamics: the worker consistently self-checked against the
explicit warning that "the supervisor re-verifies every claim", and delivered
complete round-1 work. Two honest readings: the goal prompt's framing matters
more than task shape (a worker warned of independent verification behaves very
differently from one given a bare brief), and the supervisor's verification
discipline — not rejection — carried the run's quality. Multi-round behavior
remains unmeasured.

### Mechanism incident — watcher died mid-watch (found & fixed)

During run 3, the idle watcher crashed after ~9 minutes with
`redis.TimeoutError: Timeout reading from socket` inside `pubsub.listen()`.
Root cause: **redis-py 8 defaults `socket_timeout` to 5s**, and the reference
watcher built its client without overriding it — the long blocking read died the
moment the event stream went quiet for a few seconds, killing the watcher. Had
the worker idled after that, the supervisor would never have woken: a silent
goal-mode stall. Fixed in all three reference watchers (ava-goal, ava-watcher,
ava-fleet): explicit `socket_timeout=None`, and on any remaining stream failure
the watcher falls back to polling the agents table instead of dying. The rest of
run 3 ran under the patched watcher, which then fired correctly.
