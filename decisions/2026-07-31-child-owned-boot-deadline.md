# The child guarantees "alive means progressing"; the launcher stops guessing

Follow-up to [the launch-confirm incident fix](2026-07-30-launch-confirm-respects-a-live-boot.md),
which listed this as the right end state and deferred it.

## Context

Everything an agent does before its `allocated -> starting` CAS leaves no trace:
the row reads 'allocated' with no pid whether the child is halfway through its
imports or died on the first one. The incident fix narrowed the blind spot by
asking the platform supervisor, at the deadline, whether the launched process
still exists — but "the pid exists" cannot separate a child 90% through its
imports from one deadlocked on a DB connect. So the timeout numbers stayed
guesses about how slow a loaded box gets, and an alive-but-wedged child still
burned the full extended window, out to the reap grace, before anyone noticed.

The follow-up asked for one of two things: pre-flip progress the launcher can
read, or a child that owns its own deadline.

## Decision

The child owns the deadline, and the progress signal it follows never leaves the
process.

`agent/_boot_deadline.py` arms a daemon thread in `agent/__main__.py` before the
import chain and disarms it after the CAS. The thread watches
`agent/_boot_timing.py`'s phase marks — now extended across the pre-flip segment
(`start` → `starting_import` → `schema_check` → `placement_check` →
`enter_starting`). No new phase within `AVA_AGENT_BOOT_STALL_SECONDS` and it
prints the phase the boot stalled in and `os._exit`s.

That converts the launcher's proxy into a warranted signal: within the pre-flip
window, **a process that exists is a boot that is progressing**. So
`ops/agent_launch.py` asks the supervisor on a ~1s schedule rather than only at
the deadline, and a launch that has died — crashed on import, or exited by its
own watchdog — fails in about a second instead of at the end of a window sized
for neither case. The one-shot extension and its reap-grace ceiling are
unchanged; what changed is that granting it now means something.

Three properties carry the design:

- **Milestones, not a heartbeat.** The marks are points on the boot path, so a
  wedged child cannot keep reassuring anyone — it stops emitting exactly when it
  stops progressing. A heartbeat thread would sail past a deadlock and rebuild
  the unbounded patience the incident fix rejected.
- **Patience follows progress.** Every phase resets the clock, so the number
  bounds ONE boot step rather than the whole boot — it does not have to be
  re-guessed as the import chain grows or the box gets busier, which is what
  every wall-clock window in this path had to be.
- **A second, non-resetting bound makes the total finite.** The stall window on
  its own bounds a boot only at `phases x stall`, arithmetic over a number that
  moves the day someone adds a mark — and at today's 4 pre-flip phases x 30s it
  already equals the reap grace exactly. `AVA_AGENT_BOOT_BUDGET_SECONDS` (90s)
  caps the whole pre-claim boot below `allocated_reap_grace_seconds`, and the
  child exits on whichever bound trips first, naming which.
- **The stall window sits below the confirm timeout** (pinned in
  `tests/shared/test_config.py`), so a stalled child is already gone when the
  launcher first adjudicates. Inverted, the launcher would reach its deadline
  with a wedged child still holding a pid, read that as "slow boot on a loaded
  box", and spend the reap grace on it — precisely the residual being removed.

The window reaches the child on **argv** (`--boot-stall-seconds`, from
`settings.gateway.agent_boot_stall_seconds`), not `shared.config`: importing that
module is roughly half the pre-flip import cost, so a value read from it could
only arm a watchdog after the segment it most needs to cover. argv is readable
before any import, and a timeout is not secret material — issue #974's env-only
rule governs values that are.

The child does **not** mark its own row 'terminated' on overrun, which is the one
place this departs from the follow-up's phrasing ("self-terminates the row"). A
boot wedged on the data plane cannot be relied on to write to the data plane; the
write would hang in exactly the case the watchdog exists for. Exiting always
succeeds, and it lands the launcher on its fastest existing path: a dead process
fails the confirm, which force-terminates the row with
`termination_source='launch-confirm'` and hands it to crash-resurrect. The
launcher still adjudicates, but it adjudicates a fact rather than a guess.

## Alternatives rejected

- **A pre-flip progress column on `agents_meta`** (the follow-up's first option,
  and the obvious reading of "the launcher reads the child's progress"). It
  cannot report on the segment that is actually slow. The child can only write to
  the row once its DB stack is imported and the schema gate has passed — and the
  gate must come first, or a runner whose code is ahead of the DB hits
  `UndefinedColumn` instead of the clean schema-mismatch corpse it produces
  today. Everything after that gate is milliseconds from the CAS, so every mark
  would land at once, just before the flip it was meant to precede. Measured on
  this repo: `shared.config` alone is ~0.43s of the ~0.76s `agent._starting`
  import, all of it before any row is writable. It also adds a write path for a
  row the child does not own, a schema for progress, and a migration — to observe
  the part of the boot that was never the problem.
- **A side channel (progress file next to the session record).** Would cover the
  import segment, and is the only external channel that could. But it is a second
  liveness protocol to keep correct — staleness, cleanup, cross-platform
  semantics — for a signal whose only consumer is a decision the child can simply
  make itself.
- **Keeping the deadline purely absolute, just owned by the child.** Moves the
  guess without removing it; a boot that is slow but healthy still dies. The
  stall window is the same idea made independent of total duration.
- **Letting the launcher extend indefinitely while the pid is alive.** Now that
  a live pid implies progress this looks safe, but the reaper claims the row at
  the grace, so the launcher would be waiting on a row that is no longer its own.
  The ceiling stays.
- **The stall window as the only bound.** This was the first cut of this design,
  and it is wrong for a reason worth recording: "the phase count is finite, so
  patience cannot extend forever" is true and useless, because the bound it
  yields is `phases x stall` and nothing holds that under the reap grace. The
  reaper's clock is `status_changed_at`, maintained by a trigger that fires only
  on a status flip (`db/schema.sql`), so no pre-flip signal the child could emit
  — column, file, or otherwise — would hold it off. Either the reaper learns to
  read progress, or the child guarantees it is gone first. The second is one
  constant and no new reader.

## Consequences

- `AVA_AGENT_BOOT_STALL_SECONDS=0` disables the watchdog, and doing so silently
  reverts `_launched_process_alive` to the proxy it was — the liveness probe keeps
  running and keeps believing a wedged pid. Recorded in that function's docstring
  because the coupling is invisible from the launcher's side.
- A boot step added before the CAS **must** mark a phase. An unmarked step longer
  than the stall window is indistinguishable from a wedge and will be killed as
  one. Noted in `agent/_starting.py` and `agent/startup.ava.okf.md`.
- The `boot_timing` log line's `phases` keys gained the pre-flip segments, so the
  emitted breakdown is finer than before; `enter_starting` now measures the CAS
  alone rather than everything from process start.
- Liveness is probed ~once per second per in-flight launch instead of once per
  launch. It is a session-record read plus a `kill(0)`, and only during the
  pre-claim window.
- The restarter's allocated-reaper is untouched — no new predicate, no progress
  column to read — and stays the backstop for a row whose process was never
  launched at all. It is now unreachable by a live child, because the budget
  guarantees the child is gone first, so reaper and launcher never contend.
- Ordering across four constants, pinned in `tests/shared/test_config.py`:
  `agent_boot_stall_seconds` (30) < `launch_confirm_timeout_seconds` (45) <
  `agent_boot_budget_seconds` (90) < `allocated_reap_grace_seconds` (120). The
  first inequality makes the launcher's liveness probe decisive; the last makes
  the reaper unreachable by a live child.
