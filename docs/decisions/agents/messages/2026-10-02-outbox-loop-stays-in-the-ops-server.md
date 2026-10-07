# The delivery-outbox redelivery stays in the ops server, as a resident loop

## Context

The audit proposed a `runner-maintenance` unit per agent-runner machine to collect three
disk-log or due-row replay duties that lived inside other processes: the delivery outbox
redelivery (a free-floating task in the ops server), the shell-closure notice delivery and
the impersonation event replay. The last two no longer exist: `ava stop` writes the closure
notices to the database itself in its terminals phase, and the replay consumer was deleted
with the impersonation lease log. One duty is left.

## Decision

No new unit. The redelivery becomes a resident sequential loop owned, with the ops server, by
one `TaskGroup` in the ops main function (`services/agent_ops/outbox_flusher.py`, on
`base/daemon/round_loop.py`). A loop that raises cancels the server and ends the process, and
the supervisor restarts it; the loop reports progress per record to `/healthz`.

The state of a record stays in its own file (attempts, backoff position, abandonment): the
journal must be readable when the data plane is not, since an unreachable data plane is the
usual reason a record exists. A restart therefore resumes every cooldown without a table. The
loop has no remote call of its own; a delivery is bounded by the pool wait of one flush
interval per connection, and a pass is bounded by the record cap.

## Alternatives rejected

- **A `runner-maintenance` unit for the one duty.** One more process per machine (about 50 MiB
  resident), its own pidfile, health port, pool and restart policy, for a duty that already
  shares the ops server's machine, pool and lifecycle gate.
- **Keeping the free-floating task.** It was never awaited at shutdown and its failure was
  logged and ignored.

## Consequences

- A bug that makes a flush pass raise now restarts the ops server, which the gateway dials for
  every runner RPC; a failed delivery, an unreadable record and a refused attempt are handled
  inside the pass and do not. An unreadable live config at a round start also ends the loop,
  where the old loop logged and kept its previous wait.
- A gateway-only unit runs no ops server, so it has no outbox consumer, yet the recorder's
  fields are not capability-filtered: a failed `ava agents send` there leaves a record nothing
  redelivers. Not closed here.
