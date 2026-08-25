# Turn-duration histogram ledger

The daily metrics ledger now carries a mergeable map from integer-second
duration bucket to event count. The rollup extracts the bucket in Loki with:

```logql
sum by (agent_id, bucket) (count_over_time(({<turn_end pipeline>}
  | json duration_seconds="attributes.duration_seconds"
  | line_format "{{ floor .duration_seconds }}"
  | pattern "<bucket>")[86400s]))
```

`pattern "<bucket>"` captures the rendered bare-number line as a string label;
the rollup converts it to an integer before writing JSONB. This mechanism was
validated against production Loki on 2026-08-25 for agent 3269: 47 buckets and
856 rows exactly matched client-side `floor(duration_seconds)`.

Whole-life and windowed percentiles merge the daily integer-second histogram
rows with the exact live tail; the frozen archive's exact distribution is
merged only as a raw fallback. When coverage is complete, archive-era
backfilled days contribute the same approximately ±0.5-second bucket precision
as settled days, and the exact daily min/max columns plus the archive and live
extrema continue to supply extrema, so a floor bucket cannot lower the
reported minimum.

Coverage is all-or-nothing for the requested ledger rows: every present ledger
day with turns must have a non-empty histogram, a zero-turn day is complete
without one, and absent historical ledger-gap days do not count against
coverage. Any present empty histogram on a day with turns keeps the existing
raw full-window duration pass, preserving correctness while the maintenance
daemon repairs coverage. Known limitation: a windowed read in which an
archive-era day has events but no ledger row leaves those durations out of the
percentiles; extrema are unaffected because the archive distribution still
supplies bounds.

After deployment (with the raised cap live), the hourly maintenance daemon's
first pass rerolls every retained dirty day — the `rollup_day_state` table is
empty, so each retained day is dirty — writing histograms for 2026-08-19
through yesterday (verified 2026-08-26: 08-19..08-24). A pass is bounded by
`AVA_EVENTS_ROLLUP_PASS_DEADLINE_S`, so a cold catch-up can span two passes;
days at or below the 168h retention floor are never recomputed. The earlier
2026-08-14 through 2026-08-17 ledger gap remains absent by design (08-15 and
08-17 have no ledger rows at all, verified in the DB 2026-08-26) and therefore
does not invent rows during the migration backfill.

## Loki series-cap requirement (2026-08-25 production fix)

The all-agents histogram query above returns one output series per distinct
(agent_id, bucket) pair. Loki's query-frontend series limiter
(`limits_config.max_query_series`, then 2000) counts those result series and
rejects the query when the day is busy enough — measured 2026-08-25: the
2026-08-24 query (133 agents, 29812 turns) produces 3061 series and was
rejected with HTTP 400, which aborted the entire hourly rollup pass (nothing
rolled, not just the histogram). The per-agent validation (agent 3269, 47
buckets) had never exercised the merged shape.

Fix: `max_query_series` raised 2000 → 20000 (both
`deploy/lgtm/config/loki.yaml` and `deploy/lgtm/native/config/loki.yaml`),
pinned by `shared/loki_index_labels.py:validate_loki_deploy_config` so a
re-render cannot silently drift it back. 20000 keeps ~6.5x headroom over the
busiest measured day while still blocking pathological ad-hoc fan-outs. The
rollup query shape itself is unchanged.

`max_query_series` is a static `limits_config` value — Loki does **not**
hot-reload it. The deploy must explicitly restart the Loki process (the
cluster-update service restart does this); until the restart the running
process keeps the old 2000 cap and the rollup keeps aborting every hour.

## Permanent ledger-histogram gaps (unrecoverable data)

The migration backfills `turn_dur_hist` only where the frozen `events` archive
still has rows: measured 2026-08-25, that is 2026-05-24..05-29 and
2026-08-01..08-13 (33 of 43 agents). The following days can never gain
histograms, so whole-life coverage stays incomplete and the read path keeps
its exact raw fallback for them:

- 2026-06-02..07-31 — telemetry rows pruned from the `events` archive before
  the migration (no turn_end rows remain), and Loki did not exist yet.
- 2026-08-14, 08-15, 08-16, 08-17, 08-18 and the 10 unbackfilled 2026-08-13
  agents — the post-cutover seam whose Loki data fell out of the 168h retention
  window before the histogram feature shipped; 08-15 and 08-17 have no ledger
  rows at all (verified in the DB 2026-08-26).

These days are absent from every store (PG archive, Loki, JSONL mirror);
nothing in code can recover them, and the coverage check deliberately counts
them as incomplete rather than silently dropping them.
