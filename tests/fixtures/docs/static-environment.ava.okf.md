---
type: doc
title: "Static Test Environment"
description: "Process-scoped execution for pure tool contracts: refused native data-plane effects, retained isolation guards and required CI artifacts."
tags:
- evaluation
- quality-assurance
---

# Static Test Environment

`pytest --test-environment=static` runs the paths owned by `STATIC_TEST_PATHS`:
`scripts/audit/tests`, `scripts/content_lint/tests`, `scripts/lint/tests`,
`scripts/structure/tests`, and the environment boundary and selection canaries.
Directory ownership includes descendant tests and their existing local fixtures.
These tool components inspect ASTs, files and temporary Git repositories; they
do not require a native data plane. New tests in an owned component inherit that
execution requirement and fail if they attempt Postgres or Redis access.
Explicit non-owned paths fail before collection. This process keeps unreachable
data-plane sentinels, refuses Postgres/Redis provisioners and sync/async driver
entry points, and never creates a native instance or its watchdog. Only native
provisioning and per-test data-plane resets are omitted; private-home, identity,
plugin, leak and host-effect guards remain active for the entire process.
The ownership list is an execution requirement, not a lint exemption. A parent
directory, similarly named sibling or symlink escaping ownership is refused.
Keep integration tests that require the data plane outside these owned paths;
do not temporarily enable drivers inside a static process.

Required CI runs this lane once inside `backend-structure`, with JUnit validation,
executed counts and coverage. Native full, selected and flaky commands use
`--omit-static-tests` from the same owner; combined counts and coverage include
the static artifacts. Local pytest remains native unless explicitly selected.
Nightly duration refresh still measures the full native environment, so its
static-file timings do not measure this lane's cheaper execution cost.

Isolation plugins: [[tests/docs/test-fixtures.ava.okf.md]].
