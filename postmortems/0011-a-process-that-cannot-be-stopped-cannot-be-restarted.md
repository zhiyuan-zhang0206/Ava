# 0011 — A process that cannot be stopped cannot be restarted

**Date:** 2026-10-04
**Anchors:** `services/computer/mcp_daemon.py` (`_LoopWatchdog`, `_dump_and_exit`,
`_guard_loop_liveness`, `_bounded_cleanup`, `AVA_COMPUTER_LOOP_STALL_S`,
`AVA_COMPUTER_SHUTDOWN_DRAIN_S`), `services/computer/ocr.py` (runs through
`base.host.proc.run_bounded`); supervisor side: `services/ava_root/{health,stopping}.py`.
Host-side records (supervisor log, unit output.log, diagnostic reports) are not
in this repo `(summarized)`; the wall-clock windows below are from that log.

## Summary

A computer-use call wedged the daemon's event loop, and the daemon stayed
*alive but unable to serve*: it stopped accepting, could not answer health
probes, and could not run its stop handler — Python reaches a loop-registered
signal handler only through the loop. Every root restart attempt failed at the
stop half (a blocked loop ignores SIGTERM), the restart breaker held the unit
down after five rounds, and recovery took a manual kill: ~15 minutes of
machine-wide computer-use downtime. The death left no stack, no shutdown line,
no crash report. The wedge had two halves, and the guardrails bound each: a
loop-liveness watchdog that dumps every thread's stack and exits when the loop
has been silent past a configured window, and a shutdown drain bounded by its
own deadline — because once the loop freed, the shutdown that followed
(`server.close()` had already closed the listener) *still* never finished,
which is what the "connection refused" window actually was.

## Timeline

All times host-local (the day's other evidence — bash/sleep/cp SIGQUIT
reports, brew/uv probe timeouts — shows the host itself was stalling in the
same window; the trigger was transient and did not reproduce on demand).

- 09:24:47 — the unit (pid 33198, sha `2de3326`) starts; serves AX reads and
  OCR snapshots normally.
- 09:35–09:37 — all of wave-6's earlier calls (five `ax_tree`, OCR snapshots)
  are served: the daemon was healthy entering the window.
- ~09:36:5x — an `ax_tree(include_ocr_gap=true)` call (Notes) hangs its Vision
  OCR child; the daemon's event loop is inside that synchronous call. The child
  was still in the unit's process set at 09:37:14 (`pids [33198, 56850]`).
- 09:37:04 — the health probe times out (the connect is accepted; nothing
  answers). The root's first restart follows: stopping the unit means SIGTERM,
  which a blocked loop cannot process.
- 09:37:14 — the first stop window fails: `unit computer-mcp did not stop
  within its 10s window; ownership retained` (pids [33198, 56850]).
- ~09:37:2x — the OCR child hits its 30s subprocess bound and is killed; the
  call returns `ocr_gap_error` and the loop is free again. The queued SIGTERM
  is consumed: the shutdown starts — the listener is closed and the tracked
  client handlers are cancelled.
- 09:37:5x — a caller's next `include_ocr_gap` (Finder) is reset mid-request
  (`ConnectionResetError: Connection lost` — its handler was cancelled); its
  retry is refused. From here to 09:51, every connect is refused.
- 09:38–09:50 — every connection refused; the process sits alive holding two
  connection fds to its socket path (`lsof` 12u/13u), never accepting. Four
  more restart rounds spend their stop windows the same way (a consumed
  SIGTERM no longer changes anything); 09:42:01 the restart breaker opens
  (`manual intervention needed`).
- 09:49:31 — last witness of the process: alive, fds held, every connect
  refused.
- 09:51:10 — a sample shows the main thread parked in `kevent` (an idle event
  loop) and every worker thread in `cond_wait`: nothing executing, one wait
  that never resolves — not a blocked call.
- 09:51:45–59 — the operator stop intent is recorded; `RootClient.force_down`
  (TERM → 10s window → SIGKILL escalation; 10.1s end to end — TERM alone did
  not end it) kills the process, and `up` starts the replacement (new gen
  92645; `failure state cleared after 885s`).

## Root cause

Two halves, sequential.

**1. The loop was blocked while it was still serving.** Tools execute
synchronously on one event-loop thread (an intentional serialization — the
desktop is one machine-wide resource), so any sync call that never returns
wedges the whole daemon. Leading candidate: the OCR run's own timeout path —
`subprocess.run`'s timeout kills the child and then calls `communicate()`
again *without a bound*, and a child SIGKILL cannot take down (an
uninterruptible kernel wait) blocks that second wait forever. The exact
blocking call could not be pinned post hoc (no stack was ever captured), and
the architecture admitted more than one unbounded candidate (the synchronous
psycopg audit write among them). What is proven: the loop was silent from
~09:37:04, and the first stop attempt found the OCR child still in the unit's
process set.

**2. The shutdown that followed never finished.** The "connection refused"
window — the visible failure from then until recovery — began when the queued
SIGTERM was finally consumed and the shutdown closed the listener (the reboot
of `09:37:5x` reset-then-refused is that transition). The drain then cancelled
the tracked handlers and awaited them: `await asyncio.gather(...)` followed by
`await server.wait_closed()`, with no bound of any kind. It never returned:
the shutdown line (`logger.info("shutting down")`, the statement after the
drain) never appeared in the unit's log, and the 09:51:10 sample shows why
nothing else could happen — an idle loop parked on a wait that never
resolves, with its stop signal already consumed, so every later SIGTERM was
a no-op and only SIGKILL could end it.

Which await of the drain never resolved cannot be proven post hoc (no dump
existed at that point — the watchdog's dump only fires for a *silent* loop,
and here the loop was idle, not silent). The class is known: a drain that
cancels handlers and then waits for them can be pinned by any handler that
ignores cancellation or never unwinds, and it is worse than it looks —
`asyncio.gather`'s `cancel()` only forwards into its children and leaves the
awaiting task parked on a future that never completes (CPython #32684), so
even an external timeout could not have un-parked that await. A bound there
must complete the *wait*, not merely request cancellation.

**Escape analysis.**

- *Unit tests*: they drive `_dispatch` on a faked helper — none can see "a
  supervised process that cannot die", and no test watched loop liveness or
  the stop path.
- *The healthcheck* asks "is it answering" (correctly), and a wedged loop is
  indistinguishable from a dead one at the probe — by design that triggers a
  restart, which the stop half then cannot perform.
- *The stop path* refuses (correctly, post-0010) to escalate past its window
  and retains ownership; its only exit was an operator's manual kill.
- *Observability*: the unit's log ended before the wedge; no reason line, no
  stack — the failure was invisible until a human inspected the process table.

## Guardrails added

- **Loop-liveness watchdog** (`services/computer/mcp_daemon.py`): the loop
  re-beats on each tick; a thread that sees no beat for
  `AVA_COMPUTER_LOOP_STALL_S` (default 180s) writes a reason line, dumps all
  thread stacks to stderr (the root captures the unit's `output.log`), and
  exits(1). The blocked-loop wedge degrades to a crash the supervisor
  restarts on its normal path.
- **Bounded shutdown drain** (`_bounded_cleanup`): on stop, cancel the tracked
  handlers, then wait for them with `asyncio.wait(..., timeout=drain_s)` —
  whose own timer completes the wait, where `gather`'s cancellation could not
  — and bound `server.wait_closed()` with the remaining budget. Past
  `AVA_COMPUTER_SHUTDOWN_DRAIN_S` (default 5s, deliberately below the root's
  10s stop window): dump every thread's stack and exit for a restart. The
  half-down this incident spent ~15 minutes in now ends in seconds, with the
  forensics that were missing.
- **Bounded OCR runs** (`services/computer/ocr.py`): both subprocess runs (the
  swiftc build and the recognition) go through `base.host.proc.run_bounded`,
  whose timeout kills the whole process tree and bounds its own post-kill
  drain; the call still fails softly as `ocr_error`.
- **Regression tests**: `services/computer/tests/test_computer_loop_liveness.py`
  (watchdog fires / stays silent while beaten / beats from the loop / `run()`
  arms it; drain completes cooperatively; a handler that swallows cancellation
  trips the dump), `services/computer/tests/test_computer_mcp_daemon.py`
  (run/shutdown wiring), `services/computer/tests/test_computer_ocr.py`.

Unguarded, relying on the rule alone: a loop held hostage by a C-level call
that never releases the GIL could starve the watchdog's own dump — accepted;
the watchdog covers the I/O and sleep waits this system actually performs.
The supervisor still has no force-escalation past its stop window (deliberate,
post-0010); the daemon now must not need it.

## Lessons

- A supervised server must remain able to die. If its stop handler needs the
  loop, a blocked loop is a process nobody can stop — and one nobody can stop
  is one nobody can restart.
- A shutdown drain is a deadline, not a wait. "Cancel the handlers, then await
  them" inherits every handler's unboundedness — and past the listener close,
  the process no longer looks like it is shutting down (it looks dead: refused
  connects) while it still holds everything. And the bound must be structural:
  `gather`'s cancellation cannot un-park its awaiter (CPython #32684), so the
  wait's own timer must complete it.
- A blocked process leaves no record by itself: the guardrail that acts on a
  wedge must also create the evidence (a stack dump) at that moment, or the
  next incident is re-derived from scratch.
- Give synchronous servers a liveness watchdog rather than enumerating block
  sites: bound the sites you can identify, but let the watchdog be the
  guarantee — it converts every remaining unbounded block into a crash with
  forensics.
