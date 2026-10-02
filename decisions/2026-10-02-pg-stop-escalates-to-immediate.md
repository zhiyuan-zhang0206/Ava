# A Postgres fast shutdown that does not finish is ended by an immediate one

## Context

`ava stop` asks Postgres for a fast shutdown (SIGINT) and waits for the whole process
tree to exit. A fast shutdown does not finish while the archiver is running an archive
command, and a WAL-G that never returns (a blackholed network) makes that wait unbounded.
The earlier rule ([stop window contract](2026-09-12-stop-window-contract.md)) was "no
force": the wait ran to the stop's deadline and failed with "native shutdown did not
complete; custody retained". The failure left the unit half stopped (services and pooler
down, Postgres still shutting down, hold retained), and the compensating `ava start`
could not succeed against a postmaster that is shutting down.

Measured with real wal-g (WAL-G smoke, 2026-10-02): a network blackhole makes each archive
attempt fail after about 30 s and a fast stop takes 180-186 s with three pending segments; a
wal-g that never returns blocks the postmaster for good, and it exits within 0.1 s once that
wal-g is killed.

## Decision

- **Escalate at the end of the fast budget.** SIGINT first. When the fast shutdown's
  budget is spent, the postmaster gets SIGQUIT (Postgres' immediate shutdown), then a
  bounded wait. Immediate shutdown skips the checkpoint; the next start replays WAL, and
  segments not yet archived stay in `pg_wal` for the archiver to ship after that start.
  Nothing committed is lost.
- **Kill what the archiver left behind.** The archive command is a child of the archiver
  and is orphaned when the archiver dies. The process tree is captured before the first
  signal and again before SIGQUIT; every captured process that still matches its recorded
  birth is SIGKILLed after the postmaster's wait (children first, the postmaster last
  if it is still alive). Nothing is signalled by name or by pid alone.
- **Budget.** The fast shutdown gets the stop's remaining time minus the legs after it:
  the immediate shutdown's wait, the SIGKILL wait and a cleanup wait for Redis' save.
  Those are the terminal closure's existing bounds (`PROCESS_CLEANUP_WAIT_S`,
  `PROCESS_KILL_WAIT_S`, the same values as `_TERMINAL_STOP_GRACE_S` and
  `_TERMINAL_KILL_WAIT_S`, [decision](2026-09-28-stop-escalates-to-sigkill.md)); no new
  number. A stop with less time than that reserve gives the fast shutdown all of it, and
  the escalation legs overrun the deadline by their own bounded waits.
- **Loud.** An error log, the `postgres_stop_escalated` event (level error), a line on
  stderr at the moment of escalation, and an `escalations` entry in the stop journal and
  the stop report. The stop itself completes: an escalated shutdown is not a failed stop,
  so the compensating `ava start` does not run.
- **Scope.** Only Postgres. The pooler stop and the services stop keep their
  no-escalation contract. A process that outlives its SIGKILL still fails the stop with
  custody retained.

## Alternatives rejected

- **Keep failing at the deadline.** Leaves the half-stopped unit above and needs a human to
  kill a wal-g by hand.
- **Escalate immediately, without a fast attempt.** Gives up the clean checkpoint every
  time; the fast attempt is free when the archiver is healthy.
- **Kill by name** (`wal-g`) across the host. Another home's archiver or an operator's
  wal-g would die with it.
- **Raise the 300 s deadline.** A hung wal-g never returns; a longer deadline only
  postpones the same failure.
