---
type: doc
title: "CI test selection"
description: "Backend test selection for pull requests: enforce mode runs the selected subset as the backend gate; shadow mode measures it beside the unchanged full gate."
tags:
  - ci
  - testing
---

# CI test selection

## What it is

The CI workflow selects a direct-import backend test subset for real pull
requests. In `enforce` mode (the default) a SELECTED pull request runs that
subset as its backend pytest gate; every other selector decision and every
Trunk merge-tree branch keeps the full suite. In `shadow` mode no PR gates on
the subset: the full fan-out gates and the subset plus its comparison report
run informational beside it. The workflow-level `TEST_SELECTION_MODE` value in
[ci.yml](workflows/ci.yml) is the single revert switch.

## Job data flow

1. `test-select` computes a static direct-import subset from the merge-base
   diff, publishes the active mode and an audited JSON decision
   (FULL / SKIP / SELECTED), and stays non-gating: a selector or setup failure
   leaves the routing outputs empty, which degrades to the full fan-out.
2. `backend-selected` runs only a SELECTED list and is informational in shadow.
   In enforce it is the gate, and its JUnit feeds the same Trunk quarantine
   gate as the shards (quarantined flaky failures pass, real failures block).
   `backend-shard` is skipped exactly when enforce + SELECTED; it runs the
   full suite in every other combination, including every Trunk merge-tree
   branch.
3. The backend aggregator — the branch-protection check — requires the subset
   result on the enforced path and the fan-out result otherwise. The 85%
   coverage gate runs on the full fan-out only (a subset run has no shard
   coverage artifacts).
4. `test-selection-shadow-report` compares the subset result with the full
   non-flaky pytest population and records FALSE GREEN — in shadow mode only:
   under enforce a SELECTED PR has no full pytest result to compare against.

## Related policy

[conventions/test-selection.md](../conventions/test-selection.md) owns the
selection rules, blind-file limits, duration guard, artifacts, maintenance, and
the mode switch.
