# The stop reports the pooler's remaining clients; it does not release pools

## Context

`decisions/2026-09-12-stop-window-contract.md` (point 5) had the stop release every idle
connection of the host daemon's pools and of the ops daemon's pool before the data plane
closed, through `ops.cluster_pause.release_local_db_pools`, a loopback `POST
/release-db-pools` on the host and `base.db.pool_release`. That was written for the cluster
pause of the time, which left paused runners' services running. The stop is now one local
sequence: `drain`, `quiesce`, `services`, `browser`, `terminals`, `extras`, `data-plane`, and
the `services` phase stops the whole service tree through its root owner until every process
has exited. The only caller of the release, the legacy `cluster_stop` op, went with the old
updater; nothing has called it since.

The pool release has nothing left to release on this path. A pool dies with its process before
the data-plane phase begins. A gateway's data-plane stop refuses a kept service that needs the
database, a runner-only unit stops no data plane, and PgBouncer's stop signal is SIGINT, which
disconnects clients and waits only for in-flight server transactions. What can still be
connected when the pooler stops is a runner on another machine that has not been stopped yet:
a local stop cannot reach it, and the release never did either.

## Decision

1. Delete the release: `release_local_db_pools`, the host's `/release-db-pools` route,
   `base.db.pool_release` and their tests. Point 5 of the contract is retired; the contract text
   stays as it was.
2. Before the pooler is signalled, the data-plane stop lists its clients with `SHOW CLIENTS`
   (address, database, user, state, application) and reports them: a stderr line, a log record,
   and a `pooler_clients` entry in the stop journal. A console that cannot be read is reported
   as unreadable, never as an empty list. The report changes nothing about the stop.

## Alternatives rejected

- **Wire the release into the stop as a phase between `quiesce` and `services`.** It would run
  seconds before the services phase closes the same pools with their processes, and it would
  need a new loopback route on the ops daemon for the one pool the CLI cannot reach.
- **A release endpoint on every daemon.** Every daemon that holds a pool is stopped by the
  services phase.
- **Refuse to stop the data plane while clients remain.** The incident this contract answers
  was a stop that could not complete because of clients it did not own; the report must not
  become the same wait.
- **Release from the remote runners.** A runner is stopped by its own `ava stop`; the fleet
  update stops runners before the gateway. A new cross-machine call for a step the stop
  already makes would add a way for the stop to fail.

## Consequences

- A gateway stopped while runners are still up disconnects them; the report names which
  clients were connected, which is the evidence that the order was not followed.
- The pooler's client list is read once, immediately before the signal. A client that connects
  in between is not in it.
- Idle pooled connections of a service kept running across a data-plane stop (a retained
  runner-side service on a unit whose gateway stops elsewhere) are not released by this stop;
  they are disconnected by the pooler's shutdown.
