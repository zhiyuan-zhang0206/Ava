---
type: doc
title: Tests-location lint
description: Root tests require a complete subject LCA or existing explicit policy; shared facts preserve unknown inputs and test-support boundaries.
tags:
- scripts
- lint
- tests
---

# Tests-location lint

`scripts/structure/tests_location.py` runs in pre-commit, pre-push and CI's
`pre-commit run --all-files`. A test belongs beside the code it proves. The
script's module docstring owns the rule; this document maps its consumers.

## Complete root subject proof

An unregistered `tests/**/test_*.py` may stay at root when
`scripts/structure/placement_evidence.py` proves that its resolved Python subjects have
the repository root as their nearest common ancestor directory. This query
consumes `imports.facts.collect()` and `placement.ModuleIndex`: absolute and
relative imports, aliases, supported dynamic imports and executed Python inputs
use the same dependency facts as CI impact analysis.

It removes replacement-only evidence through the existing placement policy,
without the legacy all-patch fallback. Test-support imports and resource paths
do not prove a Python subject. Bare sample strings are not facts. Every unknown,
including an unresolved resource input, prevents certification even when known
subjects already span top-level packages. Empty subjects cannot prove root.

This is a directory LCA, independent of import direction: a test of both `ava`
and `base` has root LCA even though `ava` may import `base`. The existing
private-patch home heuristic remains a separate consumer and grants no root
placement proof. Runtime fixture closure also remains separate from subjects;
a shared fixture importing an agent does not turn an SDK unit test into a root
integration test.

## Existing path policies

`tests_location_allowed.py` retains `BY_DESIGN` (e2e, browser tests, shared
fixtures and factories, existing real-process proofs) and `ALLOWED` (existing
artifact/harness contracts and integrations). These paths keep their existing
policy; this change adds no entries or baseline. Stale files, redundant entries,
unknown categories and empty reasons still fail. Future policy retirement needs
a verified replacement proof, not another registration.

## Diagnosis and scope

`--suggest PATH ...` explains the same strict subject proof. A single-component
root test names its actual subject directory and must move into that directory's
`tests/`; incomplete input names the exact unresolved site instead of guessing
an owner. After moving, preserve autouse isolation in `path_scopes.toml`.

The commit hook checks changed root tests. A change under `scripts/structure/`
widens to all tracked root tests; pre-push and CI check the same full set.
Existing path policies take the fast path. Only unregistered candidates require
facts, and the LCA reads exact dependency locations without scanning production
imports or evaluating Python. Package-local tests remain outside this gate's
scope; this capability does not force unrelated historical tests to move.

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]]. Related:
[[scripts/lint/docs/patch-targets.ava.okf.md|patch-target lint]].
