# Audit events are recorded in Postgres; Loki is a projection

## Context

The 2026-08-04 event-system decision says audit events are retained "365d+" and
append-only. That held while a Postgres table carried them. The frozen
Postgres `events` archive was imported into Loki and dropped on 2026-08-29
(task #1823), after which the 25 `category=audit` event names (plus the audit
side of `status_change`) live only in two places: Loki, whose
`limits_config.retention_period` is 84h, and the day-stamped local JSONL mirror,
pruned after 7 days. Only the lineage class (`spawn`, `fork`, `resurrect` and
their two telemetry mirrors) escapes, through a 100-year per-stream Loki rule
plus a 365-day JSONL copy.

Neither store is a system of record:

- The emitter queue sheds under overload. The audit lane blocks the producer up
  to five seconds and then drops; an event enqueued but not yet drained when the
  process dies exists nowhere.
- The OTLP export is bounded and drops (Loki 429s, circuit breaker); Loki
  truncates over-long lines, and `send_message` carries the whole message body.
- Retention expires non-lineage audit rows after 3.5 days. Readers that need
  history already degrade silently: the fleet graph and neighbor ties read the
  live Loki tail since 2026-08-13, so message-edge counts there cover only the
  last 84h, and the weekly self-evolution scan asks for a 7-day audit window.

Ava is not event-sourced: the truth about business state already lives in
Postgres business tables. What is missing is a durable record of *who did what*.
The user ruled on 2026-10-02 that audit facts are truth in Postgres and Loki is
only a projection; telemetry and log stay in Loki, unchanged.

## Decision

- One append-only table, `audit_events`, holds every `category=audit` event.
  Its columns follow the 2026-08-04 Event model (`ts`, `trace_id`, `span_id`,
  `agent_id`, `machine`, `process`, `event_name`, `level`, `source`,
  `target_agent_id`, `attributes`). UPDATE, DELETE and TRUNCATE are rejected by
  triggers, and the application roles hold only SELECT and INSERT.
- The row is written in the business transaction that produced the fact. After
  commit the same event is emitted to Loki as before, as an observation copy;
  losing that copy loses no record.
- An emit site that has no business transaction (the effect already happened, or
  another process owns it) writes the row in its own short transaction, and a
  failed write is raised, not swallowed, and never degraded to a Loki-only emit.
  Exception: an agent-facing tool call that already succeeded (for example
  `mcp_tool_call`) must not turn an audit write failure into a tool error,
  because the caller would retry and repeat the side effect. Those sites return
  the result unchanged and report the failed write loudly (error log with
  traceback plus an alert event). Internal operations raise.
- Rows are retained permanently; there is no time-based deletion and no purge
  path. If trimming is ever needed it is a new decision whose unit is a whole
  partition.
- Readers that need history (fleet graph, neighbor ties, `/api/events` with
  `category=audit`, the self-evolution scan) read Postgres. A request without a
  `category` is served by querying audit and non-audit separately and merging
  by timestamp, so its behavior does not change. Grafana panels and short-window
  rate probes keep reading Loki.
- Attributes are stored verbatim, including message bodies. Whether to store a
  hash and a reference instead is decided from measured volume.
- Pre-cutover history is backfilled once from the Loki archive stream, the live
  lineage rows and the lineage JSONL mirror, idempotently. Non-lineage audit
  rows between 2026-08-13 and the cutover are gone (expired in Loki, pruned from
  the mirror) and are accepted as lost.
- The lineage permanence machinery (the Loki `retention_stream` lineage rule and
  its deploy validation, the lineage JSONL tier, `retention_class`) and the
  emitter's blocking audit lane are removed once the readers have moved. The
  archive stream is not removed: its telemetry readers remain.

Relationship to the impersonation decision
(`2026-10-02-impersonation-event-log-in-postgres.md`): that entry makes the
impersonation hand-off record a Postgres log written at the source and states
the exception to the 2026-08-04 ruling for that record. This entry states the
principle for all audit facts. The two do not overlap: `audit_events` is global,
read by time, and holds only audit-category events; `agent_impersonation_entries`
is per-lease, read by sequence, and also holds the controller-side SDK calls,
which are not audit events. The central half of the impersonation log
references its `audit_events` row instead of storing a second copy of the body.

## Alternatives rejected

- **Keep Loki as the truth and lengthen retention.** Shedding, truncation, 429
  drops and the crash window between enqueue and drain are properties of the
  pipeline, not of the retention number; a longer window leaves the record
  incomplete.
- **Event sourcing** (derive business state from the event log). The business
  tables are already the truth, and the audit log would become load-bearing for
  every read path. Audit rows describe facts; they are not replayed.
- **Fold audit into `agent_impersonation_entries`.** That table is per-lease and
  ordered by a lease-locked sequence; audit facts are global and most are not
  attributed to any lease.
- **LISTEN/NOTIFY or Redis Streams as the record.** Neither commits atomically
  with the producing transaction, and notifications are lost while no listener is
  attached.
- **Use the identity column as a commit-order cursor.** Identity order is not
  commit order. The table has no tailing consumer; readers order by timestamp
  and use `id` only as a tiebreaker.
- **Write an intent row before every effect.** It doubles the writes to close a
  crash window that remains named instead.

## Consequences

- Every audit emit site goes through one recording primitive, and a lint rejects
  a direct audit emit that bypasses it.
- A database that cannot be written fails audit writes loudly. Processes already
  depend on Postgres for leases and checkpoints, so this adds no new single point.
- Not covered: a process that crashes after an effect but before its audit row
  (sites that write after the effect); a runner credential that forges rows
  (the two application roles share INSERT, as with the impersonation log); the
  2026-08-13 to cutover gap above.
- Storage grows with audit volume, which is measured, not assumed, after the
  cutover; the table is part of the PITR and base backups.
- The impersonation entry's sentence that the 2026-08-04 ruling "stands for
  everything else" is narrowed by this entry for audit-category events.

<!-- Superseded by: (none yet) -->
