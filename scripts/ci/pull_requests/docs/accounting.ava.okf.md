---
type: doc
title: CI usage attribution and ledger
description: "`scripts/ci/pull_requests/accounting.py` — per-agent GitHub Actions minutes: identity resolution, billable minutes, the JSONL ledger and the report rollup."
tags:
- scripts
- ci
---

# CI usage attribution and ledger

`accounting.py` reconciles CI usage to agents. GitHub's actor identity is one
shared account, so actor-level attribution does not exist; identity is parsed
from the fleet's conventions instead. Scope is the `CI` workflow only (the cost
driver — a full run is ~110 billable minutes across shards; auxiliary workflows
are seconds each). Billable minutes are measured from job started/completed
timestamps (ceil to the minute, GitHub's billing unit); GitHub's own
metrics/timing APIs are enterprise-only for this account (verified 2026-09-06).

Collection walks the window in sub-windows: the runs list endpoint caps results
at 1000 per query, and a day of the fleet can exceed that, so sub-windows keep
every run reachable. All GitHub reads are best-effort: a failed or timed-out
`gh` call yields an empty result rather than an error.

```ava-contract
id = "attribution-fallback-chain"
kind = "invariant"
statement = """A run's identity resolves in order: the real PR of a synthetic `trunk-merge/pr-<n>/<uuid>` branch (or the run's PR payload) by PR title; then a push-to-main merge run's merged PR via its merge commit; then the head commit subject; then the `ava-<id>-<slug>` head branch name. The parsed source is the legacy `[Ava-<id>]` prefix, read from PR titles and commit subjects only when present (new PR titles carry none — user ruling 2026-10-16), plus `(task #<id>)`; a run matching none stays unattributed."""
code = ["scripts/ci/pull_requests/accounting.py:_run_identity"]
tests = ["scripts/ci/pull_requests/tests/test_ci_accounting.py::test_push_run_attributes_from_commit_subject", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_branch_name_attributes_nonprefix_subject", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_trunk_branch_without_pr_payload_attributes_by_branch"]
```

```ava-contract
id = "minute-billing"
kind = "invariant"
statement = """A job's billable minutes are the ceiling of its wall duration. The jobs API reports null started/completed for in-progress and cancelled-before-start jobs; those pass through as absent and bill 0 minutes rather than crashing the timestamp parse."""
code = ["scripts/ci/pull_requests/accounting.py:job_minutes"]
tests = ["scripts/ci/pull_requests/tests/test_ci_accounting.py::test_job_minutes_null_contract", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_null_timestamps_bill_zero_and_do_not_crash"]
```

```ava-contract
id = "ledger-idempotence"
kind = "invariant"
statement = """append_ledger keys rows by run id: entries already present are skipped, so re-running a window that overlaps the ledger appends only new runs and returns the appended count (a re-fire over the same window appends 0)."""
code = ["scripts/ci/pull_requests/accounting.py:append_ledger"]
tests = ["scripts/ci/pull_requests/tests/test_ci_accounting.py::test_append_ledger_is_idempotent"]
```

```ava-contract
id = "overage-equivalent-cost"
kind = "invariant"
statement = """report_rows rolls up the last `days` per agent (runs and linux/macos minutes) and prices them at the GitHub-hosted private-repo overage rates (Linux $0.006/min, macOS $0.062/min; corrected 2026-09-06 per GitHub's published pricing). The repo is currently public, so est_usd is a private-repo overage equivalent — phase-2 quotas must gate on MINUTES, not dollars; the account's 3,000 included minutes/month are account-wide across all repos."""
code = ["scripts/ci/pull_requests/accounting.py:report_rows"]
tests = ["scripts/ci/pull_requests/tests/test_ci_accounting.py::test_report_rollup_days_and_cost"]
```

**Usage.** `accounting.py --since <iso> --until <iso> [--repo owner/repo] [--json]`
prints one attribution entry per CI run in the window; `--append-ledger PATH`
appends idempotently (the daily reconciliation step); `--report [--days N]
[--agent ID] [--json]` reads the ledger and prints per-agent totals.

Ledger rows are JSONL: `{"run_id", "day", "agent_id", "task_id", "pr_number",
"name", "head_branch", "linux_minutes", "macos_minutes", "jobs", "conclusion"}`
— `agent_id`, `task_id` and `pr_number` are null when unattributed. The default
ledger is `scripts/ci/ci_usage/ledger.jsonl`; a missing ledger file reads as an
empty ledger.
