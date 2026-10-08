---
type: doc
title: "Static Test Environment"
description: "Process-scoped execution for owned pure unit contracts: refused data-plane effects, retained isolation guards and required CI artifacts."
tags:
- evaluation
- quality-assurance
---

# Static Test Environment

`pytest --test-environment=static` runs the paths owned by `STATIC_TEST_PATHS`:
`scripts/audit/tests`, `scripts/content_lint/tests`, `scripts/lint/tests`,
`scripts/structure/tests`, `scripts/ci/tests`, `tests/ci`, `base/paths/tests`,
`base/sessions/pty/tests/test_pty_sessions_screen.py`, and the environment boundary
and selection canaries.
Directory ownership includes descendant tests and their existing local fixtures.
The tool contracts inspect ASTs, files, workflow definitions, synthetic reports
and temporary Git repositories. Path contracts operate on their private home;
the PTY screen model consumes byte/ANSI sequences through pyte without starting
a terminal service. These owners do not require a native data plane. Native
PTY service, lifecycle and database integration tests remain in their own lane.
New tests in an owned component inherit that
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

Required CI runs this lane once inside `backend-static` with four xdist
workers, each retaining the process-lifetime native refusal and isolation
guards. JUnit validation, executed counts and combined coverage include all
workers. Unhandled test-thread exceptions are errors in the actual CI command;
a refused background driver call cannot leave only a warning and green JUnit.
It runs alongside the existing structure job so tool lint and pure
unit execution do not accumulate on one runner. Native full, selected and flaky commands use
`--omit-static-tests` from the same owner; combined counts and coverage include
the static artifacts. Local pytest remains native unless explicitly selected.
Nightly duration refresh still measures the full native environment, so its
static-file timings do not measure this lane's cheaper execution cost.

Isolation plugins: [[tests/docs/test-fixtures.ava.okf.md]].
