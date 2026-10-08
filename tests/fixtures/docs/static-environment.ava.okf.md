---
type: doc
title: "Static Test Environment"
description: "Process-scoped execution for owned logging lint contracts: refused native data-plane effects, retained isolation guards and required CI artifacts."
tags:
- evaluation
- quality-assurance
---

# Static Test Environment

`pytest --test-environment=static` runs only the files owned by `STATIC_TEST_PATHS`:
the three logging lint contract files and the environment boundary canaries.
Explicit non-owned paths fail before collection. This process keeps unreachable
data-plane sentinels, refuses Postgres/Redis provisioners and sync/async driver
entry points, and never creates a native instance or its watchdog. Only native
provisioning and per-test data-plane resets are omitted; private-home, identity,
plugin, leak and host-effect guards remain active for the entire process.
The ownership list is an execution requirement, not a lint exemption.

Required CI runs this lane once inside `backend-structure`, with JUnit validation,
executed counts and coverage. Native full, selected and flaky commands use
`--omit-static-tests` from the same owner; combined counts and coverage include
the static artifacts. Local pytest remains native unless explicitly selected.
Nightly duration refresh still measures the full native environment, so its
static-file timings do not measure this lane's cheaper execution cost.

Isolation plugins: [[tests/docs/test-fixtures.ava.okf.md]].
