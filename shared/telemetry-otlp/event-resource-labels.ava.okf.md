---
type: doc
title: "OTLP event resource labels"
description: "The source-side resource grouping that keeps every Loki event_name index label equal to the unified event JSON body."
tags:
- shared
- telemetry
- otlp
- loki
---

# OTLP event resource labels

## What it is

`_EventDimensionResourceExporter` gives each event log record a Resource whose
`event_name` and optional `agent_id` equal the record's attributes. The standard
OTLP encoder partitions a batch by Resource, so every resulting `ResourceLogs`
group is homogeneous and Loki's resource-derived index labels describe every
row in that group.

## Why it exists

A `LoggerProvider` owns one static Resource while its batch processor collects
many event names. Mutating that shared Resource in the collector made the last
record processed assign an `event_name` label to the whole batch. The exporter
preserves one bounded SDK worker and moves the resource assignment to the point
where the encoder still sees each individual log record.

The event JSON body and ordinary OTLP attributes remain unchanged. Loki 3.7.6
indexes the selected Resource attributes, not log-record attributes, so this is
the write-side contract for `event_name` and the optional `agent_id` labels.

## Verification

`tests/shared/test_telemetry_otlp.py` serializes one flush containing
`llm_usage`, `node_exit`, and `log`, then asserts the resource event name equals
every record event name in every generated OTLP resource group.

Parent node: [[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP export backend & trace ship to Tempo]].
