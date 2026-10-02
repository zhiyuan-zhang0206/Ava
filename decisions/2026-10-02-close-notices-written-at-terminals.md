# Close notices are written to the database by the stop's `terminals` phase

## Context

`ava stop` closes busy persistent shells and owes each owner agent a notice
(issue #2044). The first design recorded one JSON file per notice under
`$AVA_HOME/state/pty-close-notices/` and had a task in the ops daemon deliver
the files at the next start, once the start released its maintenance hold. The
reasoning: the stop window was meant to open no database client connection
(`decisions/2026-09-12-stop-window-contract.md`), and the gateway and ops
server are already down when terminals close.

The contract's constraint is on background loops: ownership renewal, page
reconciliation and the notice flush must not borrow a pool across the window,
because paused runners' idle pooled clients were what held PgBouncer's stop
open. It does not forbid the stop command's own writes — the same stop already
writes the host posture row through the database before the services stop. And
the `terminals` phase runs before the `data-plane` phase, so the database is up
when the notices are known.

## Decision

The `terminals` phase writes each notice itself, over one short connection that
is opened after the closure and closed before the phase returns: no pool, no
background task, no file. A gateway unit dials Postgres directly (its pooler
stops next); a runner-only unit has no local data plane and dials its
configured URL — the gateway's database, with its own runner login, the dial
its posture write already made. No notice means no connection. Nothing writes
after the data-plane phase starts. A notice that cannot be written is printed
on stderr with its agent and session, and does not fail the stop: the closed
session's record is gone on any retry, so failing the stop would restore
nothing. The inbound row is pending — the owner's agent host is down, so
the canonical insert's best-effort wake finds no listener; its next start
claims the row.

The disk journal, the ops daemon's delivery task and the quiesce-wait it needed
are deleted. Notices a previous stop left on disk are not imported or
delivered; the directory is inert. The delivery outbox's local journal is
unchanged: its trigger is the write channel itself failing, which has no
database to write to.

## Alternatives rejected

- **Keep the file journal and its flush.** Two machines' worth of machinery
  (atomic files, a retry ladder, an idempotency claim to survive a crash
  between commit and delete) for a write the stop can make directly while the
  database is still up.
- **Route runner notices through the gateway API.** A runner's stop already
  needs the database to be reachable (the posture write precedes the services
  stop), so a second path would only cover the window in which the gateway
  disappears between those two points — and it would fail in the same
  situation.
- **A one-time import of old journal files.** Informational notices, written
  by the previous release's stop; the cost is machinery that exists only to be
  deleted.

## Consequences

- A notice can be lost when the database becomes unreachable between the
  posture write and the terminals phase; it is loud, not silent.
- The stop's `terminals` phase may spend up to the connection timeout waiting
  on an unreachable database when notices exist.
- The rollout that ships this change is stopped by the previous release's
  code, which still writes files: notices for busy shells closed in that one
  stop are not delivered.
