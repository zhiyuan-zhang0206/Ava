---
type: doc
title: "CI test selection"
description: "Informational direct-import backend test selection for real pull requests, measured beside the unchanged full backend gate."
tags:
  - ci
  - testing
---

# CI test selection

## What it is

The CI workflow has a shadow-only test-selection path for real pull requests.
It preserves the existing backend aggregator, branch-protection check, coverage
artifacts, and full pytest fan-out. Trunk merge-tree branches always keep the
full suite.

## Job data flow

1. test-select computes a static direct-import subset from the merge-base diff
   and exposes an audited JSON decision.
2. backend-selected-shadow runs only a SELECTED list, captures every pytest
   exit status, and uploads test-selection-subset-log without failing its job.
3. test-selection-shadow-report records the existing backend aggregator result,
   compares the subset with backend-shard's matching non-flaky pytest
   population, uploads test-selection-shadow-report, and emits FALSE GREEN only
   when that population fails after a passing subset.

All three jobs are informational, including setup and artifact failures. They
are deliberately absent from the backend aggregator dependencies, branch
protection, and Trunk configuration.

## Related policy

[conventions/test-selection.md](../conventions/test-selection.md) owns the
selection rules, blind-file limits, duration guard, artifacts, maintenance, and
the separate enforcement-switch criteria.
