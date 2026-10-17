---
type: doc
title: "Nightly Test Duration Refresh"
description: "Daily CI-shaped isolated pytest timing measurements saved as an Actions cache that every CI run restores as .test_durations."
tags:
  - ci
  - testing
  - automation
---

# Nightly Test Duration Refresh

`workflows/refresh-test-durations.yml` runs daily at 19:30 UTC and on manual
dispatch. It re-measures the 8 backend and four e2e CI shard shapes on isolated
runners, each shard seeded with the current timings and using the matching CI pytest
arguments, recording a clean duration artifact and retrying independently when a
timing-sensitive failure occurs.

The publish job accepts only the complete 12-artifact set, merges it into
`.test_durations` and saves that file as the Actions cache
`test-durations-<date>-<run id>-<attempt>` on main; a verify job lists the newest
cache, restores its exact key and checks the contents. Nothing is committed and no
pull request is opened. CI's `classify` job resolves the newest entry once per run
(`actions/resolve-test-durations`) and each shard restores that exact key
(`actions/restore-test-durations`); with no entry the run warns, shards split by test
count and the selector keeps the full suite. The file is gitignored.

## Key dependencies

- [[.github.meiva.okf.md]] — parent overview and the CI shard definitions.
- [[../scripts/docs/scripts.meiva.okf.md]] — the measurement and merge command.
- [[../tests/docs/tests.meiva.okf.md]] — pytest-split duration consumers.
