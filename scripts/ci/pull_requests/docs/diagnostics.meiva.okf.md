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

```meiva-contract
id = "failure-signature-table"
kind = "invariant"
statement = """_FAILURE_SIGNATURES is ordered and first match wins: each entry is (regex over the failing log, classification, suggested action) matched case-insensitively; with no match, a check whose name contains "lint" classifies as a deterministic lint failure and anything else as unclassified. The classification and action strings are the operator-facing diagnosis text."""
code = ["scripts/ci/pull_requests/diagnose.py:_FAILURE_SIGNATURES"]
tests = ["scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_lint_hard_limit", "scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_test_failure_uses_native_evidence", "scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_runner_network_flake"]
```

```meiva-contract
id = "best-effort-evidence"
kind = "invariant"
statement = """Every remote read degrades instead of erroring: a failed or timed-out `gh` call, unreadable JSON or a non-zero exit yields None or empty text, never an exception; failed per-job log enrichment leaves the rollup's classification standing, and a PR whose view cannot be read still returns a diagnosis carrying `gh_error`."""
code = ["scripts/ci/pull_requests/diagnose.py:_pr_view", "scripts/ci/pull_requests/diagnose.py:_job_log_tail"]
tests = ["scripts/ci/pull_requests/tests/test_command_timeouts.py::test_best_effort_reads_keep_their_timeout_outcomes"]
```

```meiva-contract
id = "pr-level-issues"
kind = "invariant"
statement = """For an OPEN PR: mergeable=CONFLICTING reports merge_conflict; a recorded base behind main reports stale_base with both shas; an unreadable base freshness reports base_unreadable rather than silence. Merged and closed PRs have mergeable=UNKNOWN and a base behind main by definition — neither is reported as a problem."""
code = ["scripts/ci/pull_requests/diagnose.py:_pr_level_issues"]
tests = ["scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_merge_conflict", "scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_stale_base", "scripts/ci/pull_requests/tests/ci_monitor/test_queue_ops_exclusive_with_merge_wait.py::test_diagnose_merged_pr_reports_no_conflict_or_stale_base"]
```
