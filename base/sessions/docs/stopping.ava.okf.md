---
type: doc
title: "Stopping a process — the kill contract and the non-session trio"
description: "How Ava stops what it started: `kill_session`'s (ok, mode) contract and its graceful/forced escalation for named sessions, `base/host/proc.py`'s `process_alive` / `request_stop` / `force_kill` trio for processes that are not sessions, how a stop converges a service tree whose leader already died, and what the deadline report names when convergence fails."
tags:
- base
- process
- supervision
- stop
---

# Stopping a process

## What it is

The primitives for ending a process Ava started. Normal `stop`
uses `cli/commands/lifecycle/service_stop.py`: deliver a verified graceful signal and
wait for actual process exit without implicit escalation. Its shared deadline
reports an incomplete stop if resources remain. Persistent terminals are the
exception: a stop HUPs/TERMs each shell's
captured session and SIGKILLs what outlives a bounded grace
([[base/sessions/pty/docs/session-kill.ava.okf.md|session kill]];
docs/decisions/2026-09-28-stop-escalates-to-sigkill.md). Explicit force may use a
backend's `kill_session`; non-session processes use `base/host/proc.py` primitives.
The lower-level escalating APIs below retain their own explicit contracts.

## Core responsibilities

### Kill contract

`kill_session(name, graceful=...)` → `(ok, mode)` with mode in `{graceful, forced, noop}`. `mode` reports what **happened**, not what was requested: a graceful stop that had to escalate to the SIGKILL fallback returns `forced`, so the caller's escalation marker fires instead of a clean-stop one that hides a hard kill. Idempotent: an absent/dead session is a `noop`. `ok` means **the session is confirmed gone**, not "the kill command was accepted" — backends re-ask their own existence check after killing, because a kill that reports success it did not achieve turns a live-but-unbacked session into a service nothing starts (issue #1015). `graceful=True` SIGTERMs only the top process and waits up to the timeout, then hard-kills the tree; `graceful=False` SIGKILLs children first so a parent cannot respawn a child mid-teardown. `expected=True` marks an operator-initiated transition (rollout/update/stop) so backends that escalate a kill log at INFO instead of WARNING/ERROR there.

Signal delivery follows the verified launch shape. POSIX launchers exec into
the daemon, so SIGTERM reaches the recorded PID directly.
`base/daemon/shutdown.py` maps the service stop signal to the daemon's
KeyboardInterrupt cleanup.
The ops daemon explicitly cancels and awaits loop tasks before its final exit,
without joining stuck executor threads. See [[session-backend.ava.okf.md|session backend]].

### Stop convergence when a service leader is gone

`cli/commands/lifecycle/service_stop.py` treats a confirmed-dead leader as one step,
not the end of the stop. A live leader still runs its own graceful cleanup
first; once the leader is confirmed dead, its captured, birth-validated
descendants are signalled — at most once each, SIGTERM on POSIX — so a
descendant that outlived a non-forwarding launcher cannot hold the stop open.
Refusals stay refusals: a descendant that will not accept the signal keeps the
hold and is reported at the deadline — no implicit SIGKILL, no certificate
while it lives.

The retry handle is the spawn-time process group on the session record
(`SessionRecord.pgid`): a record whose leader died is retained and listed
exactly while that group still has members, so a stop re-entered after the
incident converges the orphan through the recorded group instead of certifying
the unit stopped. Legacy records without a `pgid` reap as before.

### The deadline report when convergence fails

A held stop that runs its deadline out must not leave the operator with a bare
pid list — that is a diagnosis no one can act on, and the only remaining move
is a blind rerun (issue #2162). `cli/commands/lifecycle/_maintenance_stop_report.py`
builds the failure report instead: for every process still alive (and every
member of a recorded process group that is still occupied, even one that
appeared after the capture) it records the owning recorded session, whether the
process is that session's leader, a captured descendant, or a group member, the
birth pair the stop path itself revalidates, and the best-effort cmdline —
plus the stop stage (the phase label) that hit the deadline. The services stop
raises it as `StopIncompleteError` (a `TimeoutError`, so every existing catch
keeps working). The terminal closure reports a process that outlived its
SIGKILL the same way, at stage `terminals` (`close_terminals`).

Reads are best-effort but never dishonest: a process that cannot be inspected
is listed as unreadable rather than dropped, and a PID recycled since capture
is never described with its new occupant's facts. Nothing in the report path
signals.

The report is persisted, not just printed. The printable message carries the
inventory inline, and when the stop owns the lifecycle journal the same data
lands on `$AVA_HOME/run/lifecycle-op.json` as structured fields
(`result.stage` + `result.survivors`, one JSON object per process), so the
diagnosis survives the process that printed it and a later operator can read
it back.

### Stops that do not go through a session

Not every process Ava stops is a named session: the pooler, an orphan holding a unit port, the gate daemon. Those go through `base/host/proc.py`'s trio — `process_alive` (probe) / `request_stop` (ask) / `force_kill` (force). `cli/commands/data_plane/pgbouncer.py:stop_pgbouncer` captures the exact pooler owner and delegates to its native custodian; an incomplete stop retains custody and fails. Normal stop instead uses the data-plane boundary in `cli/commands/data_plane/maintenance_stop.py`: it does not escalate the pooler, and ends a Postgres fast shutdown that outlives its share of the budget with an immediate shutdown (see the 2026-10-02 decision). A pid this user may not signal is handled the same way on all three legs: alive, undeliverable, reported as a survivor — never an exception out of the middle of a stop. Same file: `kill_process_tree` (parent + descendants, enumerated before the kill) and `run_bounded` (a timeout that bounds the work, not just the wrapper).

## Entry points

- `cli/commands/lifecycle/_maintenance_stop_report.py` — the deadline survivor report (raise, render, journal payload)
- `base/sessions/backend.py:SessionBackend.kill_session` — the session stop, per backend
- `base/host/proc.py:process_alive` / `request_stop` / `force_kill` — the non-session trio
- `base/host/proc.py:kill_process_tree` / `run_bounded` — tree teardown and a bounded run
- `cli/commands/data_plane/pgbouncer.py:stop_pgbouncer` — exact pooler custody and bounded stop

## Notes

- The trio keeps liveness, graceful delivery, and force-kill behavior consistent
  across the stop paths that use them.
