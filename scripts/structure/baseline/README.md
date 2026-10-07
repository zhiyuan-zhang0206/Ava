# Structure baseline shards

This directory holds the structure lint's frozen baseline, split into one
shard file per directory area — `<shard>.json`, named after the first two
components of an entry's directory (`agent/graph/x.py` files under
`agent.graph.json`; a `directories` entry's key is itself the directory, so
`base` lives in `base.json`). See `scripts/structure/baseline_shards.py`
for the exact rule (`shard_of`) and `scripts/lint/code_structure.py` for how
the shards are merged, validated and compared against the base revision. An
existing entry may remain in its original shard when its file moves. Duplicate
section/key pairs across shards fail the gate; the growth guard still checks
the combined entries and permits no additional targets or counts.

`rules.json` is not a shard: it records the rule version a section was frozen under.
The guard still checks each key and count when that version changes; a rule upgrade
or a newly introduced lint cannot add exemptions.

This directory is temporary while the existing exemptions are removed. Keep its
README while the shrink-only gate still reads it. After the final exemption is
fixed, remove the baseline directory and its supporting machinery instead of
keeping an empty baseline or allowing new exemptions to be frozen.
