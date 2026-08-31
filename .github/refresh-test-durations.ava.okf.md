---
type: doc
title: "Nightly Test Duration Refresh"
description: "CI-shaped isolated pytest timing measurements that publish complete .test_durations updates through a review-only pull request."
tags:
  - ci
  - testing
  - automation
---

# Nightly Test Duration Refresh

`workflows/refresh-test-durations.yml` runs daily at 19:30 UTC and on manual
dispatch. It re-measures the 12 backend and four e2e CI shard shapes on
isolated runners. Each shard uses the matching CI pytest arguments, records a
clean duration artifact, and retries independently when a timing-sensitive
failure occurs.

The merge job accepts only the complete 16-artifact set. It atomically updates
`.test_durations` after dropping sub-0.2-second entries, so a failed or missing
shard cannot publish a partial timing model. This preserves CI's environmental
isolation rather than running the whole suite in one timing-coupled runner.

When timings change, the workflow force-updates the review-only
`ava-bot/test-durations` branch and opens or updates the fixed refresh PR. It
does not auto-merge that PR.

## Key dependencies

- [[.github.ava.okf.md]] — parent overview and the CI shard definitions.
- [[../scripts/scripts.ava.okf.md]] — the measurement and merge command.
- [[../tests/tests.ava.okf.md]] — pytest-split duration consumers.
