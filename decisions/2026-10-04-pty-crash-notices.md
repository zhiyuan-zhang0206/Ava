# A service that died uncleanly tells its sessions' owners through a one-shot child

## Context

The pty-sessions service holds no database client on purpose
([2026-10-03-pty-sessions-service](2026-10-03-pty-sessions-service.md)), so only `ava stop` wrote
closure notices. A service crash, a SIGKILL of the service and a reboot ended every session without a
word to its owner: the master closes, the shells are hung up, and the agent finds a dead session on its
next call with no reason attached. The ledger sweep at the next start already knew which busy sessions
it closed and only logged them.

## Decision

1. The start-time sweep reports every session the ledger last saw running a job, not only the ones it
   still had a process to close: after a crash or a reboot the hangup has usually ended all of them
   already, and those owners lost a job all the same. A session that was only an idle shell is never
   reported. The ledger is a snapshot taken every ten seconds, so a job that finished in that window is
   a false positive, which is why that notice says "probably interrupting".
2. The service gives the sweep's busy sessions to a one-shot child, `python -m ops.pty_close_notices`,
   from a task beside the serving loop, with a thirty-second limit. The child reads them on stdin,
   writes the notices over one pooled connection and exits. The service stays database-free; the
   child has no process profile, so it dials as an operator process does, the way `ava stop` does.
3. A database that cannot be reached costs a log line and nothing else. The service starts and serves
   either way; nothing retries, because the sweep already cleared the sessions from the ledger.
4. One reason, `CRASH_REASON`, covers a crash, a forced stop of the service and a reboot, because the
   sweep cannot tell them apart. It carries no operation and no hold time. A `ava stop` that finds no
   service and closes from the ledger uses the same reason, since the service died before that stop.
5. Delivery stays idempotent on (machine, agent, session, shell birth), the same key `ava stop` uses,
   so a shell birth is told once whichever path reaches it first, and an owner that is terminated or
   unknown is dropped, never resurrected.

## Alternatives rejected

- **A database client in the service.** The reason it has none is unchanged: a wedged pool or a slow
  database must not stall the loop that answers every agent's shell. A child confines both.
- **Write the notices before the service serves.** It makes the start wait for the database, and on a
  reboot the data plane often comes up after the service. The notices are a side channel; the serving
  loop is not allowed to depend on them.
- **Retry until the database is back.** It needs a persisted queue next to the ledger. A lost notice
  after a crash that already lost the session is the smaller harm; the idempotency key would make a
  retry safe if that ever changes.
- **Notices for `ava stop --force` with the service up.** Force is usable offline by definition and
  skips the drain guarantees; it stays without notices. Only the sweep paths, where the service died
  uncleanly, notify.

## Consequences

- A reboot can lose the notices when the data plane is not up yet when the service starts. The child
  logs it; the owner finds the session gone without a reason, as before this change.
- A machine's `.env` must be readable by the child as by any `python -m` process: it needs the
  machine name and the database it is told about in its environment.

Partly superseded by: [A crash-notice batch is staged on disk and re-sent, not lost to the
child's time limit](2026-10-04-pty-crash-notices-staged-retry.md) — the child's batch is
staged before it runs and a batch it does not finish is re-sent at the next start, replacing
the "nothing retries" consequence above.
