# Launch-confirm yields to a live boot instead of counting seconds

## Context

An agent's row goes `allocated` at spawn and the child itself CASes it to
`starting` (`agent/_starting.py:enter_starting_state`). Everything the child does
before that CAS — python startup, its import chain, `assert_schema_current`, the
placement SELECT — is invisible: the row reads `allocated` with no pid whether the
child is halfway through its imports or died on the first one.

The launcher waited a fixed 10s for that CAS and, on expiry, forced the row
`terminated`. Under heavy box load the pre-flip segment ran past 10s, so the
launcher took the row from a child that was seconds away from claiming it. The
child's CAS then matched 0 rows and it exited; crash-resurrect brought the agent
back — into the same 10s window, four attempts each, spaced by the 300s resurrect
backoff. One agent took 7 minutes and ~7 processes to start.

Two distinct defects. The window was too small for a loaded box, and the
retries-exhausted terminate write (`ops/agent_launch.py:_launch_or_force_terminated`)
carried no status predicate — the only terminate-write in the codebase without one
— so it clobbered whatever the row had become, including `starting` under a live
pid. That one manufactured duplicate launches: the buried agent was running, and
`launch-confirm` is a crash-resurrect-eligible source, so a second process was
started for it.

## Decision

1. Guard the write: `... WHERE id = %s AND status = 'allocated'`, matching every
   sibling. "All attempts failed" is the launcher's local view, not ownership of
   the row.
2. `launch_confirm_timeout_seconds` 10 → **45**, `allocated_reap_grace_seconds`
   60 → **120**. The invariant the grace's own description states — it must exceed
   boot plus the confirm window — is now pinned by a test rather than by prose in
   two files that do not reference each other.
3. Make the deadline load-tolerant, not merely larger: at expiry the launcher asks
   the platform supervisor whether the process it launched is still alive (the
   session record under `$AVA_HOME/run/sessions/`, which carries pid + start time).
   Alive → **one** extension, to the reap grace measured from the start of the
   wait. Dead → fail immediately, unchanged.

The extension's ceiling is the reap grace on purpose: past it the restarter's
allocated-reaper claims the row, so a longer wait would be a wait on someone
else's row. Reaper and launcher never contend.

## Alternatives rejected

- **Only raise the timeout.** Any fixed number is wrong on a machine whose load is
  not fixed; it just moves the cliff. It also trades away detection latency for a
  genuinely dead launch — which is the case the poll was built for (agent 137/44).
  The liveness probe separates the two cases instead of averaging them.
- **A child-owned deadline / pre-flip progress heartbeat.** The right end state:
  the process that knows how its boot is going says so, and the launcher stops
  guessing. It is also a protocol — a write path before the row is owned, a schema
  for progress, a story for a child that heartbeats and then wedges. Out of scope
  for an incident fix; filed as a follow-up, with this entry as its context.
- **Keep extending while the pid stays alive.** Unbounded patience for an
  alive-but-wedged child (a deadlocked DB connect holds a pid forever), and it
  would outlive the reaper's claim on the row.
- **Drop the launch-confirm and lean on the reaper.** The confirm is what turns a
  failed launch into an immediate, attributable `launch-confirm` corpse that
  crash-resurrect retries; the reaper is a 30s-cadence backstop with a coarser
  story. Losing the fast path to fix its deadline is backwards.

## Consequences

- A launch that fails for real is detected in 45s instead of 10s, and a
  resurrect/respawn call blocks that much longer per attempt (they already run off
  the event loop). Accepted: the failure is rare, the false positive was not.
- The retry sequence (4 attempts, ~187s with the new window) can now outlast the
  120s grace, so the reaper may take a row mid-retry. That is a legitimate reap of
  a row nothing has claimed, and the guarded write means the launcher no longer
  fights it — but it does mean "retries exhausted" and "reaped" can both appear
  for one launch.
- The launcher now depends on the supervisor's session record being accurate. It
  already is the basis of `ava stop`'s no-DB reap, and pid recycling is defeated by
  the recorded start time.

---

The deferred follow-up landed:
[the child owns its boot deadline](2026-07-31-child-owned-boot-deadline.md). The
liveness probe described above is unchanged, but "alive" now implies "progressing"
— so it is consulted on a schedule rather than only at the deadline, and the
alive-but-wedged case named in the rejected alternatives no longer waits out the
extension.

Forward link (2026-08-22): launch confirmation now observes pid claim rather
than a historical status transition; see
[agent status model](../docs/history/2026-08-22/agent-status-model.md).
