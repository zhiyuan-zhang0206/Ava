---
type: doc
title: "Whole-file Collection Shadow"
description: "Opt-in whole-file shard evidence: complete snapshots and paired Linux execution, resolved fixtures and coverage."
tags:
- infrastructure
- quality-assurance
---

# Whole-file Collection Shadow

`scripts/ci/file_shard_shadow.py` is an opt-in pytest plugin for evaluating
collection before sharding. Planning and ordinary checking require `--collect-only`.
An explicit `--file-shard-execute` opts into running a checked group for runtime
proof, requiring a per-worker runtime report. Loading the plugin without its
options leaves collection unchanged. Required CI routing remains unchanged.

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
runtime `getfixturevalue`, import side effects and order dependence need execution
evidence. Synthetic contracts exercise directory fixtures, parameters, dynamic
collection and resolved runtime bindings with real xdist workers.

## Paired Linux runtime proof

`Whole-file runtime proof` runs by manual dispatch or on changes to this proof's
code and workflow. It pins all jobs to the same source SHA, snapshots once, then
runs the existing node split and checked whole-file split sequentially on each
of 16 runners, with the same four workers, native environment and coverage
sources. Neither population retries. The baseline always runs first; this
experiment establishes execution equivalence and reports collection/test-phase
costs, but one pair does not establish a statistically stable latency gain.

Each worker records setup/call/teardown outcomes and times, plus the actual
resolved fixture name, implementation and scope after the call. This includes
`getfixturevalue()` bindings through pytest's pinned-version fixture request
state. Repeated reports for the same node and phase fail immediately, including
repeated execution inside one worker. Worker reports never share an output
filename. The planning job uses the same native binaries and vendored runtime
as the paired runners, preserving environment-dependent collection. Group checks must match
their planned node IDs and declared closure before any test body executes.

`scripts/ci/file_shard_runtime.py` requires all worker and controller reports,
zero exit statuses, version/configuration/duration agreement, exclusive execution
and the exact complete planned node population. Runtime fixture bindings and
outcomes must match between populations, including skip outcomes. Both coverage
populations are combined separately; the source files and executable statements
must match, and losing any baseline covered line fails the proof. Additional
covered lines remain visible in its report. Missing artifacts, crashes, fixture
changes and coverage loss cannot produce a successful comparison artifact.

This proof is not a required-gate migration. A successful Linux run, fresh
complete timings and review of actual file-group execution balance are needed
before changing the regular backend shards. Plans remain disposable snapshots;
never reuse a successful old plan after source changes.

Infrastructure owner: [[scripts/docs/scripts.ava.okf.md]].
