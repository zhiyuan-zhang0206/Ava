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

appended_runs counts the ledger rows the reconciliation added (0 = the re-fire
no-op). One debt_sweep_daily event per claimed 06:30 cluster-time slot reports
scan_status as an explicit fact: the mechanical scan may fail without blocking
the clearing worker, so the failure is never implied away by the worker action.

pr_flow_run is the point-in-time Trunk queue-depth sample; it is emitted even
when the depth sample is unavailable, so the daily breadcrumb survives a Trunk
outage, and the missing field stays absent.

ci_runs_run is one breadcrumb per repository: the fixed window's population and
this run's GitHub-read budget — current observations, so gauges even though
their names are counts.

The remaining understanding events: an enqueue failure is best-effort (the next
trigger covers the same stretch); a queued chunk skipped without a node was
already described or had no conversation content; an upper-level grouping
failure leaves the level's open nodes ungrouped until the next threshold of new
nodes; a snapshot-lag event marks a compaction whose boundary wait timed out
(the closing chunk may find the snapshot short); and a failed call record is
reported while the node or group the call produced is kept.
