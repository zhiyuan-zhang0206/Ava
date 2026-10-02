# Impersonation events are logged in Postgres at the source

## Context

The 2026-08-04 event-system decision moved audit and telemetry out of Postgres
tables into one stream read back from Loki, and the emitter's header says the
retired Postgres archive is never revived. Impersonation hand-off then adopted
that stream as its source of truth: the SDK calls and audit events attributed
to a borrowed agent identity are read back from Loki and written into the
session's permanent entries, so the resumed agent can be told what the takeover
did.

Loki cannot carry that duty. The emitter queue sheds telemetry-category events
(`sdk_call`) under overload, a central audit event that commits but is never
emitted has no copy anywhere, Loki has no sequence number, ingestion lags, and
retention can expire an envelope before it is read. The protocol built to
compensate (manifest census, freeze, replay sweeps over timestamp windows, a
second Loki read for certification, an external certifier holding a secret, a
retention-loss state) is about 1.2k lines in three files plus a 481-line
migration, eight `impersonation_event_*` settings and about 1.8k lines of
dedicated integration tests, and it was only a week old and still changing.
Postgres already held the identity ledger (event key and digest per event); only
the event bodies lived in the lossy store.

The user ruled on 2026-10-02 that impersonation delivery needs an integrity
guarantee, accepts a Postgres log written at the source as an exception to the
2026-08-04 ruling, and freezes new work built on "Loki is the truth".

## Decision

The impersonation record of SDK calls and audit events is written to Postgres by
the producer, into `agent_impersonation_entries`, the session's existing
immutable, sequenced, permanent log.

- A central audit event is appended in the producing transaction, under the lease
  row lock that already decides whether the lease still admits events.
- A controller-side SDK event is appended synchronously at the existing capture
  seam, before the telemetry queue. It cannot share a transaction with the call's
  effect, so it is write-after-effect; a hard crash between effect and write is
  exposed as a pending delivery by a per-source seal count, never as a silent gap.
- Delivery is complete when a predicate over the rows holds inside the database:
  the lease has ended, admission is closed, every participant is sealed, and each
  seal count equals the rows of that source. No external system takes part, so no
  external certifier, certification secret or replay loop exists.
- The consumer is the hand-off export, a one-shot reader ordered by the lease's
  `seq`. There is no persistent consumer cursor and no ack column.
- Rows are permanent like every other entry. If trimming is ever needed the unit
  is a whole lease, because a partial deletion would void the completeness
  predicate.
- Loki still receives the same events as an observation copy. Only the
  impersonation record moves; the 2026-08-04 ruling stands for everything else.

This is the outbox pattern without a relay: the event row commits with the state
change, and the consumer reads the table directly instead of a broker.

## Alternatives rejected

- **Keep Loki as the truth and harden the protocol.** Every defect above is a
  property of the store (shedding, no sequence, retention, lag), so each fix is
  another compensation layer.
- **A new dedicated table with a global monotonic id.** `agent_impersonation_entries`
  is already per-lease, immutable, granted to the runner role and read by the
  export. A serial id is not commit-ordered, so a cursor over it can skip a row
  that commits late; `seq` is allocated under the lease lock, so commit order is
  `seq` order.
- **LISTEN/NOTIFY.** Notifications are lost while no listener is attached, and
  the default transaction-mode pooler rules out LISTEN on ordinary dials. The need
  is a durable log, not a signal.
- **Redis Streams.** A second store cannot commit atomically with the producing
  Postgres transaction, which brings the dual-write problem back.
- **Write an intent row before each SDK call.** It doubles the writes to close a
  window the protocol already reports honestly, for a promise the hand-off never
  made (it certifies emitted events only).

## Consequences

- The manifest census, replay, reconciliation loop, Loki comparison and retention
  probes, the certification secret and its finalizer ticket, and seven of the
  eight settings are deleted; `max_items` becomes a code constant and
  `seal_wait_seconds` remains as the in-process drain wait.
- New automatic leases are log-native. Manual leases stay out of scope, and no
  earlier lease is converted or replayed.
- Not covered, and documented as such: a controller hard-crashing between a call's
  effect and its row leaves the lease pending, not self-healing; SDK sampling still
  happens before capture, so a sampled-out call exists in no log; a shared runner
  credential can still forge entries, as before.
- The per-event write already existed as the identity-ledger insert, so database
  load is unchanged in kind.

<!-- Superseded by: (none yet) -->
