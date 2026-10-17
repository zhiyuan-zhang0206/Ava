---
type: doc
title: CI diagnosis and monitor contracts
description: "`scripts/ci/pull_requests/diagnose.py` — the `scripts/ci/cli.py --diagnose` classifier: the ordered failure-signature table, the best-effort read policy, and the PR-level issue rules of the monitor suite."
tags:
- scripts
- ci
---

# CI diagnosis and monitor contracts

`diagnose.py` classifies a PR's failing CI checks from the rollup, job log
tails and Trunk state. Diagnosis only — no repair action is taken here; the
operator executes the suggested action. Evidence sources: the PR's check
rollup + job log tails, Trunk's queue state, and the synthetic trunk-merge
test PRs, one per queue attempt — their failed logs are where queue-attempt
failures are read.

The suite's usage-ledger fixtures fix their day relative to now: the
`--ci-usage` rollup window is a rolling `--ci-usage-days` x 24h, so a literal
"recent" day ages out (2026-09-13: literal fixtures crossed midnight UTC and
turned every backend shard red).

Every remote read degrades instead of erroring: a failed or timed-out `gh` call, unreadable JSON or a non-zero exit yields None or empty text, never an exception; failed per-job log enrichment leaves the rollup's classification standing, and a PR whose view cannot be read still returns a diagnosis carrying `gh_error`.
