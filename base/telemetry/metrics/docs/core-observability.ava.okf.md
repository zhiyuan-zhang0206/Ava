---
type: doc
title: Core observability queries
description: Event-stream measurement scope and compaction effectiveness queries.
tags: [contracts]
---

# Core observability queries

## Query measurement scope

```ava-contract
id = "query-measurement-scope"
kind = "invariant"
statement = "Core observability queries read declared event payloads and usage-time cost snapshots. Exec outcomes exclude exec_envelope; unknown exec spellings remain visible. Missing/unknown syntax-fix and halt labels belong to their other buckets."
code = [
  "base/telemetry/metrics/core/observability.py:core_metrics",
]
tests = [
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_cost_queries_unwrap_cost_usd",
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_exec_breakdown_covers_every_outcome",
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_syntax_fix_kind_buckets",
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_halt_breakdown_buckets",
]
```

The pack is admitted by the core metric catalog under plugin `core`, using the same query validation as plugin metrics. LogQL selectors match indexed event names and agent identities; JSON decoding supplies flattened `attributes_*` fields. Selector regexes are full-string matches, unlike substring line filters. The execution envelope measures transfer activity, not an outcome; counting it as an unknown failure would dominate the outcome graph. Multi-series targets need explicit legend names because aggregate queries carry no identifying labels. Historical production row counts are not current contracts.

## Compaction effectiveness denominator

```ava-contract
id = "compaction-effectiveness-denominator"
kind = "invariant"
statement = "The soft share denominator is completed compact_summary plus auto events carrying reminder_shown. Manual requests and historical unattributed events are excluded. The preparation target counts only compact_summary with reminder_shown=true; proactive summaries alone cannot satisfy it. No soft completions yield zero when the denominator exists, while no completions yield no data."
code = [
  "base/telemetry/metrics/core/observability.py:core_metrics",
]
tests = [
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_soft_compaction_share_excludes_manual_and_unattributed_history",
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_preparation_target_requires_a_reminder_in_the_completed_window",
  "base/telemetry/metrics/core/tests/test_core_observability.py::test_completed_paths_separate_reminder_coverage_from_hard_fallback",
]
```

The size-ratio query collapses per-event label fan-out with `avg`, avoiding sparse single-point series. Completed counts separately show proactive, reminded, hard-with-reminder and hard-without-reminder paths. The >=95% target still requires runtime evidence; static query checks establish its expression and scope, not achieved behavior or the quality of saved files.
