---
type: doc
title: Maintenance and understanding event payload semantics
description: ci_usage_daily reconciliation, CI run/PR-flow absolute gauges, and the understanding chunk-gap, rebuild, leaf-order and backlog facts.
tags: [contracts]
---

# Maintenance and understanding event payload semantics

Daily reconciliation, PR-flow and CI-run samplers emit absolute state: every run
re-emits its trailing window, so each numeric field becomes an OTLP
ObservableGauge — a counter or histogram would accrue across re-emissions — and
a field whose sample is missing stays absent rather than reporting zero.

```ava-contract
id = "ci-usage-reconciliation"
kind = "invariant"
statement = """One ci_usage_daily event per reconciliation day (05:00-05:00 cluster time): the CI workflow's runs and ceil-billed minutes for the window, split by attribution and OS; the attribution source is the legacy [Ava-<id>] prefix, read from PR titles and commit subjects only when present (new PR titles carry none — user ruling 2026-10-16). est_usd is the private-repo overage equivalent at the GitHub-hosted rates; the repo is currently public, so minutes are the billing fact. Per-agent detail lives in the attribution ledger, not here."""
code = ["base/events/declarations/maintenance.py:CiUsageDaily"]
tests = ["schedules/tests/test_c9_daily_report.py::test_window_bounds_covers_the_slot_day_in_cluster_timezone", "schedules/tests/test_c9_daily_report.py::test_summarize_splits_attribution_and_meters_minutes"]
```

appended_runs counts the ledger rows the reconciliation added (0 = the re-fire
no-op). One debt_sweep_daily event per claimed 06:30 cluster-time slot reports
scan_status as an explicit fact: the mechanical scan may fail without blocking
the clearing worker, so the failure is never implied away by the worker action.

```ava-contract
id = "pr-flow-daily-gauge"
kind = "invariant"
statement = """PrFlowDaily is one event per complete cluster-tz day in the trailing window, re-emitted on every run so the whole window stays inside Prometheus retention; every numeric field is absolute per-day state (an ObservableGauge), never a sum, and fields are absent when the day has no such sample (no merges -> no percentiles; an unreachable flaky source omits flake_new_quarantines rather than claiming zero)."""
code = ["base/events/declarations/maintenance.py:PrFlowDaily"]
tests = ["base/tests/telemetry/otlp/test_event_contract_otlp.py::test_pr_flow_payloads_and_metric_dispositions"]
```

pr_flow_run is the point-in-time Trunk queue-depth sample; it is emitted even
when the depth sample is unavailable, so the daily breadcrumb survives a Trunk
outage, and the missing field stays absent.

```ava-contract
id = "ci-runs-daily-dedup"
kind = "invariant"
statement = """One absolute-state ci_runs_daily sample per complete cluster-time day and repository. Classification labels overlap; white_run_share alone deduplicates instant skips, superseded runs and abandoned PR heads in that priority order. Per-PR percentiles are absent when no completed PR has attributed runs. first_pass_pr_share is v1's run-level approximation: attributed non-noise, non-zombie runs have no red conclusion and no retry attempt."""
code = ["base/events/declarations/maintenance.py:CiRunsDaily"]
tests = ["scripts/ci/pull_requests/tests/test_ci_runs_export.py::test_daily_aggregates_deduplicate_white_runs_and_pin_first_pass"]
```

```ava-contract
id = "ci-workflow-window"
kind = "invariant"
statement = """ci_workflow_window reports trailing-window absolute workflow state keyed by repository and workflow name, emitted with the same daily sampler. Execution percentiles omit zombie wall-clock artifacts and zero-execution runs rather than treating an empty population as a real zero."""
code = ["base/events/declarations/maintenance.py:CiWorkflowWindow"]
tests = ["scripts/ci/pull_requests/tests/test_ci_runs_export.py::test_workflow_window_uses_non_zombie_execution_and_pr_appearance"]
```

ci_runs_run is one breadcrumb per repository: the fixed window's population and
this run's GitHub-read budget — current observations, so gauges even though
their names are counts.

```ava-contract
id = "understanding-chunk-gap"
kind = "invariant"
statement = """understanding_chunk_gap reports the stretches of a chunk that no node will describe — the runs after the first uncovered run of a chunk that overlapped existing nodes, and the turns a closing chunk's boundary snapshot lacks. The rest of the chunk was described."""
code = ["base/events/declarations/maintenance.py:UnderstandingChunkGap"]
tests = ["base/agents/history/hierarchy/tests/test_chunk_consumer.py::test_existing_nodes_in_the_middle_leave_a_gap_that_is_reported_not_dropped"]
```

```ava-contract
id = "understanding-rebuild-failure"
kind = "invariant"
statement = """understanding_rebuild_failed marks a rebuild closed as failed after its last allowed attempt; the agent's leaves stay as they were (no partial replacement), and the failure is never silent."""
code = ["base/events/declarations/maintenance.py:UnderstandingRebuildFailed"]
tests = ["base/agents/history/hierarchy/tests/test_rebuild.py::test_a_failing_rebuild_is_retried_then_failed"]
```

```ava-contract
id = "understanding-leaf-out-of-order"
kind = "invariant"
statement = """A level-1 node that landed before one that already has a parent reports understanding_leaf_out_of_order; nothing repairs it automatically and the agent's upper levels need a manual build."""
code = ["base/events/declarations/maintenance.py:UnderstandingLeafOutOfOrder"]
tests = ["base/agents/history/hierarchy/tests/test_rebuild.py::test_a_leaf_landing_before_a_grouped_one_is_reported_and_only_a_queued_rebuild_closes_it"]
```

```ava-contract
id = "understanding-backlog"
kind = "invariant"
statement = """understanding_backlog samples the chunk queue once per consumer round: rows waiting, rows in flight, and how long the oldest waiting row has waited. A growing age is a consumer that does not keep up."""
code = ["base/events/declarations/maintenance.py:UnderstandingBacklog"]
tests = ["base/agents/history/hierarchy/tests/test_chunk_consumer.py::test_the_backlog_event_carries_in_flight", "base/agents/history/hierarchy/tests/test_chunks_queue.py::test_backlog_counts_pending_and_running_and_ages_the_oldest_pending"]
```

The remaining understanding events: an enqueue failure is best-effort (the next
trigger covers the same stretch); a queued chunk skipped without a node was
already described or had no conversation content; an upper-level grouping
failure leaves the level's open nodes ungrouped until the next threshold of new
nodes; a snapshot-lag event marks a compaction whose boundary wait timed out
(the closing chunk may find the snapshot short); and a failed call record is
reported while the node or group the call produced is kept.
