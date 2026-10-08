---
type: doc
title: "Whole-file Collection Shadow"
description: "Opt-in collection-only evidence for whole-file shards: a complete snapshot, pytest-split balancing and per-group node/fixture comparisons."
tags:
- infrastructure
- quality-assurance
---

# Whole-file Collection Shadow

`scripts/ci/file_shard_shadow.py` is an opt-in pytest plugin for evaluating
collection before sharding. Both planning and checking require `--collect-only`;
neither changes the required CI jobs or executes a test body. Loading the plugin
without its options leaves collection unchanged.

## Snapshot and ownership

The plan starts with the real eligible `session.items`, after marker and other
collection filters. Every node records its repository-relative file and declared
fixture closure. Whole files are indivisible units, with each file and node
owned by exactly one group. Paths, duplicate ownership, unknown fields and
non-finite/negative durations are rejected. Collection errors never produce a
valid partial plan. Each invocation clears its previous output before collection
or snapshot validation; failed runs cannot leave an old successful artifact.
Plan and report paths must differ.

Both the existing node baseline and candidate file groups use pytest-split's
`LeastDurationAlgorithm`. File weights sum the relevant known node durations;
unknown nodes use the same relevant-node mean as pytest-split. Fast and zero
durations remain measurements. The report includes the unknown population and
both estimated loads; estimates do not prove actual execution balance.

## Run a shadow comparison

Use an isolated checkout with its own environment. For the full native backend
population, excluding the serial and static lanes:

```bash
.venv/bin/python -m pytest -c pyproject.toml --rootdir=. --collect-only -q --ignore=tests/e2e \
  -m "not flaky" --omit-static-tests -p scripts.ci.file_shard_shadow \
  --file-shard-count=16 --file-shard-plan=/tmp/backend-file-plan.json

.venv/bin/python -m pytest -c pyproject.toml --rootdir=. --collect-only -q -m "not flaky" \
  --omit-static-tests -p scripts.ci.file_shard_shadow \
  --file-shard-check=/tmp/backend-file-plan.json --file-shard-group=1 \
  --file-shard-report=/tmp/backend-file-check-1.json
```

Pin both `-c` and `--rootdir`: existing external option-path arguments can affect
pytest's early configuration discovery, and rootdir alone does not select the
configuration file. Its digest includes the file, ini overrides and selection
filters, so drift is rejected before collecting a candidate group.
Check every group from that plan, keeping the same checkout, selection filters,
environment, pytest/pytest-split versions and duration input. The checker derives
its file arguments from the plan and compares the resulting nodes and fixtures
against that group's complete-collection snapshot. Missing, extra or changed
nodes leave a diagnostic report and fail. A legacy `--splits` invocation is
rejected because it would compare an already reduced population.

Plans are temporary evidence, never a committed or reusable discovery cache.
Regenerate after source changes: checking planned files alone cannot discover
new files outside the snapshot. The fixture comparison covers declared closure;
runtime `getfixturevalue`, import side effects and order dependence still need
execution evidence. Synthetic contracts execute the candidate files to verify
directory fixtures, parameters and dynamic collection; full runtime and Linux
cost validation remain prerequisites for changing the gate.

Infrastructure owner: [[scripts/docs/scripts.ava.okf.md]].
