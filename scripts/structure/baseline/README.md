# Structure baseline shards

This directory holds the structure lint's frozen baseline, grouped into ordinary
component folders: `base/db.json`, `agent/graph.json` and `cli/commands.json`.
A single-component area remains a top-level shard, such as `agent.json`.
The shard name derives from the first two components of the entry's directory;
readers also accept historical flat names such as `base.db.json`. See
`scripts/structure/baseline_shards.py`
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

File, directory, complexity and nesting budgets have no baseline sections.
These sections are retired and cannot be added again, even as empty objects.
