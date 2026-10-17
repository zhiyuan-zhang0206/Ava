---
type: doc
title: CI Usage Accounting
description: Complete GitHub evidence, per-agent attribution and append-only usage history.
tags: [ci, accounting]
---

# CI Usage Accounting

`accounting.py` collects completed `CI` runs in `[since, until)` using raw GitHub
JSON. PR associations remain objects with a `number` field through attribution.

Collection returns only after every requested page and selected run's jobs are complete. Query failures, malformed responses (including absent or empty workflow names), unstable or truncated pagination, and unfinished evidence raise CiUsageReadError. A completed empty response is valid; incomplete results never reach the CLI's append operation.

Errors retain the endpoint or operation and failure category, without command
environment, response bodies or stderr. The CLI exits 1; C9's existing handler
reports failure before append or success summary. There is no automatic replay:
recollect the same explicit window after GitHub has complete evidence.

An executing run makes the window incomplete: skipping loses it as the daily
window advances; storing zero makes an unfinished observation permanent.
There is no persisted intermediate state or retry framework.

Workflow-run searches use at most 100 records per page and expose at most 1000 matching results for filtered searches. The collector splits the requested window into four-hour segments and refuses a segment whose reported total reaches 1000, requiring a narrower explicit window instead of claiming complete evidence from a possibly capped total.

Ordinary PR associations read their real PR title; a trunk-merge/pr-N branch uses its real PR instead of its synthetic association. Main runs may resolve their merge commit's PR, then attribution falls back to the commit subject for `(task #N)`. A failed title query is missing evidence, not a successfully unattributed run.

Task references `(task #N)` in PR titles and commit subjects supply task attribution without distinguishing internal vs external agents. Fallbacks resolve absent attribution; they do not disguise a failed authoritative lookup.

Only completed jobs contribute final minutes. A skipped or cancelled job that never started is a valid zero, as is a completed run with an empty complete jobs response. Started jobs require ordered timezone-aware start and completion timestamps; unfinished or malformed evidence cannot become a zero-minute ledger entry. Valid durations round up to whole minutes.

Row fields, the `macos` job-name platform split and private-repository cost
coefficients are unchanged. Costs are estimates, not a current pricing claim.
Report tests use relative dates so fixtures do not age out.

[GitHub jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run)
default to `filter=latest`; completeness here concerns that selected execution,
not all historical attempts. Existing `run_id` idempotency cannot add a rerun
after that run was recorded. Historical rerun accounting needs a separate
ledger decision; recollection does not replace an existing row.

## Operator entry

```sh
.venv/bin/python scripts/ci/pull_requests/accounting.py --since START --until END --repo OWNER/REPO
.venv/bin/python scripts/ci/pull_requests/accounting.py --since START --until END --append-ledger scripts/ci/ci_usage/ledger.jsonl
.venv/bin/python scripts/ci/pull_requests/accounting.py --report --days 7 --json
```

The ledger reader used by the read-only CI CLI keeps its existing interface.
The daily C9 schedule imports this module through its repository package; its
materialized script location does not change the accounting owner.
