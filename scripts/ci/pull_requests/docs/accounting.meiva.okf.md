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

**Usage.** `accounting.py --since <iso> --until <iso> [--repo owner/repo] [--json]`
prints one attribution entry per CI run in the window; `--append-ledger PATH`
appends idempotently (the daily reconciliation step); `--report [--days N]
[--agent ID] [--json]` reads the ledger and prints per-agent totals.

Ledger rows are JSONL: `{"run_id", "day", "agent_id", "task_id", "pr_number",
"name", "head_branch", "linux_minutes", "macos_minutes", "jobs", "conclusion"}`
— `agent_id`, `task_id` and `pr_number` are null when unattributed. The default
ledger is `scripts/ci/ci_usage/ledger.jsonl`; a missing ledger file reads as an
empty ledger.
