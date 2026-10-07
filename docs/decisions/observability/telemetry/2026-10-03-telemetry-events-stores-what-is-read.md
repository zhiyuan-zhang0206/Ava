# `telemetry_events` stores the events somebody reads, not every event

## Context

[`2026-10-02-telemetry-events-in-postgres.md`](../../data/database/2026-10-02-telemetry-events-in-postgres.md) put
every telemetry and log event in `telemetry_events` on the ruling "when in doubt it is
persisted". [`2026-10-03-telemetry-readers-on-postgres.md`](../../data/database/2026-10-03-telemetry-readers-on-postgres.md)
then moved every aggregate reader onto the table. With the readers known, most rows have none:
`exec_envelope`, `hook_timing`, `delta_read_compat`, `node_exit`, `sdk_call`, `text`, info-level
`log` and similar events are written on every turn and no Postgres reader asks for them by name.
The JSONL mirror and Loki keep every event.

## Decision

- `EventSpec.persist` (default false) marks an event a Postgres reader queries by name or prefix.
  `base.telemetry.event_store.is_persisted(event_name, level)` is the one judgment: stored when
  the spec says `persist`, when the level is warning or higher (the resolution counts, the stats
  and the status page read by level), or when the name is not registered.
- Three entrances apply it: the emitter's live sink, the mirror replay (`jsonl_rows`) and the
  backfill script. Missing one would bring the filtered rows back through a replay.
- A guard test (`base/events/tests/test_persisted_events_cover_readers.py`) collects the names the
  readers query (the SQL literals of every module that reads the table, the name lists the readers
  pass, the inspector metrics) and requires `persist=True` for each. A new reader that forgets the
  mark fails the test instead of reading an empty set.
- The two readers whose only source was a non-stored event are removed: the `/api/metrics`
  `sdk_usage` section (`sdk_call`) and the `node_exit` branch of the observation recovery scan.
  The live observation projection still reads `node_exit` from the emitter.
- Rows already in the table stay; it is append-only.

## Alternatives rejected

- **Keep storing everything.** The cost is rows no reader will ever select, on the hottest write
  path, for a table with no delete path.
- **Filter by tier.** The `noise` tier contains events readers do query (`code`, `halt`,
  `syntax_fix`); tier is a display property, not a reader inventory.
- **Filter at the readers' side only** (leave the table complete, query with a name list). The
  point is the write volume; the filter has to sit in front of the insert.
- **A separate persist list outside the registry.** The declaration already carries the event's
  other properties, and a list elsewhere drifts from it.

## Consequences

- `/api/events?trace_id=` returns the stored events of a turn, not its whole chain. The whole chain
  is in Loki within its 84-hour window and in the JSONL mirror beyond it; `inspect-a-trace` says so
  and reads Loki alongside Postgres.
- Per-agent `events` counts and first/last timestamps in `/api/metrics` count stored events only.
- Observation rows for `node_exit` are no longer recoverable from the table after a projection gap.
