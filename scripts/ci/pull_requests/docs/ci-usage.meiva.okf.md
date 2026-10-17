---
type: doc
title: CI Usage Accounting
description: Complete GitHub evidence, per-agent attribution and append-only usage history.
tags: [ci, accounting]
---

# CI Usage Accounting

`accounting.py` collects completed `CI` runs in `[since, until)` using raw GitHub
JSON. PR associations remain objects with a `number` field through attribution.

```meiva-contract
id = "complete-github-evidence"
kind = "invariant"
statement = "Collection returns only after every requested page and selected run's jobs are complete. Query failures, malformed responses (including absent or empty workflow names), unstable or truncated pagination, and unfinished evidence raise CiUsageReadError. A completed empty response is valid; incomplete results never reach the CLI's append operation."
code = ["scripts/ci/pull_requests/accounting.py:CiUsageReadError", "scripts/ci/pull_requests/accounting.py:_gh_output", "scripts/ci/pull_requests/accounting.py:_gh_api", "scripts/ci/pull_requests/accounting.py:_read_page", "scripts/ci/pull_requests/accounting.py:_pages", "scripts/ci/pull_requests/accounting.py:collect", "scripts/ci/pull_requests/accounting.py:main"]
tests = ["scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_invalid_workflow_name_cannot_append_a_partial_window", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_run_pagination_uses_raw_pages_before_filtering_ci", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_jobs_pagination_counts_every_completed_job", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_changing_or_duplicate_pages_refuse_incomplete_evidence", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_exclusive_until_does_not_finalize_the_next_window_run"]
```

Errors retain the endpoint or operation and failure category, without command
environment, response bodies or stderr. The CLI exits 1; C9's existing handler
reports failure before append or success summary. There is no automatic replay:
recollect the same explicit window after GitHub has complete evidence.

An executing run makes the window incomplete: skipping loses it as the daily
window advances; storing zero makes an unfinished observation permanent.
There is no persisted intermediate state or retry framework.

```meiva-contract
id = "github-page-limit"
kind = "external"
statement = "Workflow-run searches use at most 100 records per page and expose at most 1000 matching results for filtered searches. The collector splits the requested window into four-hour segments and refuses a segment whose reported total reaches 1000, requiring a narrower explicit window instead of claiming complete evidence from a possibly capped total."
source = "https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository"
applies_to = "GitHub REST workflow-run searches using the created filter"
code = ["scripts/ci/pull_requests/accounting.py:_WINDOW_CHUNK_SECONDS", "scripts/ci/pull_requests/accounting.py:_pages"]
tests = ["scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_runs_over_github_search_limit_require_a_narrower_window", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_run_pagination_uses_raw_pages_before_filtering_ci"]
```

```meiva-contract
id = "attribution-sources"
kind = "invariant"
statement = "Ordinary PR associations read their real PR title; a trunk-merge/pr-N branch uses its real PR instead of its synthetic association. Main runs may resolve their merge commit's PR, then attribution falls back to the commit subject for `(task #N)`. A failed title query is missing evidence, not a successfully unattributed run."
code = ["scripts/ci/pull_requests/accounting.py:_identity_from_prs", "scripts/ci/pull_requests/accounting.py:_run_identity"]
tests = ["scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_actual_github_envelope_retains_pr_identity", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_pr_title_failure_is_not_an_unattributed_success", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_trunk_synthetic_pr_resolves_to_real_pr", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_main_push_run_resolves_pr_via_merge_commit", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_branch_name_attributes_nonprefix_subject"]
```

Task references `(task #N)` in PR titles and commit subjects supply task attribution without distinguishing internal vs external agents. Fallbacks resolve absent attribution; they do not disguise a failed authoritative lookup.

```meiva-contract
id = "completed-job-minutes"
kind = "invariant"
statement = "Only completed jobs contribute final minutes. A skipped or cancelled job that never started is a valid zero, as is a completed run with an empty complete jobs response. Started jobs require ordered timezone-aware start and completion timestamps; unfinished or malformed evidence cannot become a zero-minute ledger entry. Valid durations round up to whole minutes."
code = ["scripts/ci/pull_requests/accounting.py:job_minutes", "scripts/ci/pull_requests/accounting.py:_completed_job_minutes"]
tests = ["scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_unfinished_evidence_cannot_become_a_final_zero", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_completed_never_started_jobs_keep_legitimate_zero", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_completed_run_without_jobs_is_a_complete_zero", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_invalid_job_timestamps_are_not_a_valid_zero", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_missing_start_evidence_is_not_a_never_started_job", "scripts/ci/pull_requests/tests/test_ci_accounting.py::test_pr_run_attribution_and_minute_split"]
```

```meiva-contract
id = "preserve-ledger-history"
kind = "invariant"
statement = "The JSONL ledger remains append-only and idempotent by run_id. Collection finishes before append begins, so a failed collection leaves existing bytes unchanged and does not append earlier successful rows from that collection. Existing historical rows are not automatically repaired or replaced."
code = ["scripts/ci/pull_requests/accounting.py:append_ledger"]
tests = ["scripts/ci/pull_requests/tests/test_ci_accounting.py::test_append_ledger_is_idempotent", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_failed_or_incomplete_jobs_never_append_a_zero_entry", "scripts/ci/pull_requests/tests/test_accounting_integrity.py::test_later_run_failure_does_not_append_an_earlier_complete_run"]
```

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
