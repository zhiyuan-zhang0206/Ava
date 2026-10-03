# Telemetry and log events are recorded in Postgres; Loki is an observation copy

## Context

[`2026-10-02-audit-events-in-postgres.md`](2026-10-02-audit-events-in-postgres.md) made
`category=audit` a Postgres record and left telemetry and log in Loki. The run timeline, the
inspector's metric panel and `/api/events` read telemetry (`llm_usage`, `turn_end`, `exec*`,
the LLM and stream anomalies, `meta` aggregates) and log rows from Loki, whose
`limits_config.retention_period` is 84 hours. A turn older than three and a half days vanished
from the timeline, and the run timeline's lifecycle lookback said "Loki retains seven days"
while Loki kept 84 hours, so its default window start was wrong. The user ruled on 2026-10-02:
everything the run timeline and the inspector show must be persisted, and when in doubt it is
persisted; the table can be trimmed later if it grows too large.

## Decision

- One append-only table, `telemetry_events`, holds every `category=telemetry` and `category=log`
  event. Its columns are `audit_events`' plus `category` and `cluster`. It is partitioned by month
  on `ts` (UTC boundaries), so trimming later is dropping a partition. There is no retention
  policy and no delete path; UPDATE, DELETE and TRUNCATE are rejected by triggers and the
  application roles hold SELECT and INSERT only. A partitioned table's unique key must contain
  the partition key, so identity is `(event_uid, ts)`; `ts` is part of the id's input, so a
  redelivered event repeats both.
- The emitter's drain thread appends every batch right after the JSONL mirror write. The producer
  never waits. The sink uses its own one-connection pool, short statement and lock timeouts, and
  backs off (2 s doubling to 60 s) after a failure. The first failure and every 50th after it log
  an error and emit a `telemetry_store_failed` anomaly. A process that holds no database
  authority turns the sink off.
- The mirror is the fallback and the repair source. The events-maintenance daemon replays this
  machine's full mirror every hour from a saved byte position, so a database outage and the
  daemons without a database login (gate, memory search, the browser daemons) are covered on the
  gateway machine. Other machines' mirrors and the Loki windows go in through the operator backfill
  `scripts/data_repair/backfill_telemetry_events.py` (dry-run by default, idempotent,
  `imported_from` names the source).
- Partitions are created by the writer itself through a SECURITY DEFINER function
  (`ensure_telemetry_event_partitions`), because the application logins have no DDL. A DEFAULT
  partition catches an event outside every month so a write never fails; a backfill reaches back
  over older months first, since a month cannot be created over rows in the default partition.
- Readers: `/api/events` (the telemetry and log branch), `/api/agents/{id}/events`,
  `/api/cluster/admin/events`, the run timeline and the inspector's metric panel read Postgres.
  `/api/events` merges `audit_events` and `telemetry_events` as it merged audit and Loki. The
  inspector's metric templates are the Grafana LogQL; an evaluator for the registry's own
  vocabulary answers them on the two tables over the fixed 24 hours in hourly steps, and a template
  outside that vocabulary is reported on its own metric. Attribute filters compare as text, as the
  Loki reader did, so a JSON number matches its string form.
- Loki and Prometheus keep receiving the same events for Grafana and short-window probes.
- Still on Loki, unchanged: the stats dashboard's token and cost tails, the `/api/metrics`
  aggregates, the ops monitor series, the events-maintenance rollup and resolution passes, and
  the Grafana panels. Each is a windowed aggregate with its own cache and budget and is moved by
  its own change.

## Alternatives rejected

- **Lengthen Loki's retention.** The queue sheds, OTLP export drops and Loki truncates long
  lines; none of that is a record, and a longer window only moves the cliff.
- **Write telemetry in the producer's transaction, as audit does.** Telemetry has no business
  transaction to join, and a producer must never wait on the database for an observation.
- **A table per event family or typed columns for `attributes`.** The attributes differ per event
  and per release; one JSONB column is the shape the stream already has, and the readers filter on
  keys they name.
- **Index `attributes` with GIN.** The write rate and size are high and every current reader
  filters on a few named keys after the agent and name indexes; revisit with a measured query.
- **Deduplicate the `msg` and `body` attributes of `exec` and `code` events.** Rows are stored
  verbatim, as for audit; the duplication is about a fifth of the raw bytes and is the first thing
  to measure if the table must be trimmed.

## Volume

Measured on 2026-10-02 from the JSONL mirrors of four machines (macmini, the gateway host, two
company machines; three more were unreachable), telemetry plus log, complete UTC days
09-26 to 10-01: about 164 thousand rows and 125 MB of raw JSON per day on average, 221 thousand
rows and 175 MB over the last three days, 229 thousand and 196 MB on the peak day. About 89 % of
the bytes are telemetry. `exec` and `code` bodies are about 45 % of the bytes on the two busiest
developer machines. At 0.6 to 1.0 of raw size plus row overhead and indexes (about 30 %), that is
roughly 3.6 to 5.5 GB per month and 43 to 67 GB per year, before WAL, vacuum and backups. TOAST
does not compress rows under 2 KB, so the compression factor is the least certain input.

## Consequences

- Telemetry history is permanent and queryable; Loki's 84 hours no longer bound any run timeline,
  `/api/events` window or inspector panel.
- A deployment must run the backfill once before the readers are relied on for the days Loki and
  the mirrors still hold; older telemetry is gone.
- A database outage costs the live rows of that period until the daemon's replay (this machine) or
  the backfill (others) lands them; the mirror keeps them for seven days.

Superseded in part by [`2026-10-03-telemetry-readers-on-postgres.md`](2026-10-03-telemetry-readers-on-postgres.md): the readers listed as still on Loki now compute on `telemetry_events`.
