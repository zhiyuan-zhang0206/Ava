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

Whole-life percentiles merge the frozen archive's exact distribution, complete
daily histogram rows, and the exact live tail. Windowed requests use the ledger
histogram plus the same tail. Ledger-covered buckets trade precision for bounded
reads: their percentile contribution has approximately ±0.5-second precision;
the exact daily min/max columns continue to supply extrema, so a floor bucket
cannot lower the reported minimum.

Coverage is all-or-nothing for the requested ledger rows. Every present ledger
day must have a non-empty histogram; absent historical ledger-gap days do not
count against coverage. Any present empty histogram takes the existing raw
full-window duration pass, preserving correctness while the ledger repairs.

After deployment, the hourly maintenance daemon self-heals histogram coverage
for 2026-08-17 through yesterday within one hour. The earlier 2026-08-14 through
2026-08-16 ledger gap remains absent by design and therefore does not invent
rows during the migration backfill.
