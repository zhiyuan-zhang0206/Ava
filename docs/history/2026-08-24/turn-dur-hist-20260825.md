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

After deployment, the hourly maintenance daemon self-heals histogram coverage
for 2026-08-17 through yesterday within one hour. The earlier 2026-08-14 through
2026-08-16 ledger gap remains absent by design and therefore does not invent
rows during the migration backfill.
