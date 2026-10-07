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

`rules.json` is not a shard: it records the rule version a section was frozen under
(`baseline_shards.py` explains how the guard uses it when a rule changes).

This README is committed even when every shard is empty (all structural debt
paid off): git does not track empty directories, so without it a fully clean
baseline directory would vanish from the tree and become indistinguishable
from a revision that predates the sharded baseline entirely — the very commit
where the shrink-only guard should start comparing against an empty baseline.
Keeping this file here means the directory, and an empty baseline, are always
visible to git.
