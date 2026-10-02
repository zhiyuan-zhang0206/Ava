# The delivery watchdog's recovery jobs are resident sequential loops in one service

## Context

The watchdog's three RPC-driven recovery jobs (terminated-owner resurrect retry,
stalled crash-marked harvest request, hosted-turn wedge recovery) were spawned every
tick as free-floating tasks. Single flight was a process-local dict of in-flight
tasks, and the per-agent cooldown and the resurrect failure and suppression-escalation
counters were process-local dicts too, so a restart zeroed all of them and the first
tick retried every candidate at once. The question was whether each job becomes its own
service, or all three fold into the existing one.

## Decision

`delivery_watchdog` stays one service. The three jobs are three resident loops, each
strictly sequential (one round at a time, so an agent is never in two attempts at once
and no single-flight registry exists), owned together with the scan loop by one
`TaskGroup` in the service's main function. A loop that raises cancels its siblings and
ends the process; the supervisor restarts it. Within a round the per-agent RPCs run
under a round-scoped `TaskGroup`, bounded by a semaphore and a per-RPC deadline.

Cooldowns and the resurrect failure and suppression counters live in
`delivery_watchdog_attempts`, one row per (loop kind, agent). An attempt is claimed with
one statement that checks the cooldown and stamps the clock, and the clock restarts when
the attempt ends, so a restart resumes every cooldown. Each loop reports its own progress
to `/healthz`.

## Alternatives rejected

- **One sequential loop with three phases.** One slow RPC in any phase delays the other
  two.
- **Three services.** Full isolation and separate restarts, but three more pidfiles,
  health ports, pool connections and roster, port-table and health-check entries, and
  about 145 MiB of resident memory, for a failure domain the three jobs share (the same
  tables, the same database). The jobs are separate functions with their state in the
  database, so splitting later moves loops into entry points without a data migration.
- **Keeping the in-memory dicts.** A restart would still re-attempt everything at once.

## Consequences

- A round blocks until its RPCs finish (each bounded by the deadline), so one slow
  unreachable machine holds up its own loop's next round, not the other loops.
- One migration (`delivery_watchdog_attempts`) and a service restart roll this out.
- A database outage skips a round; any other exception in a loop restarts the service.
- Stopping one job without restarting the service still needs a config gate; only the
  harvest job has one.
