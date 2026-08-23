# Event-name resource labels

## Context

Loki indexes the OTLP resource attributes selected by its configuration, but an
OTLP logger provider has one shared resource while the SDK batches many log
records. The collector transform that copied each record's `event_name` into
that shared resource therefore made a batch's last event name label every row.

## Decision

The emitter now gives every OTLP log record a resource containing its own
`event_name` and, when present, `agent_id` immediately before the SDK encodes
the export batch. The SDK partitions its `ResourceLogs` by resource, so the
Loki index label and the unchanged JSON event body agree for every row without
adding an exporter worker per event type.

The pinned Loki 3.7.6 OTLP schema was also checked: it can promote resource
attributes to index labels but not log-record attributes. The collector's
`groupbyattrs` processor could re-associate the records downstream, but the
emitter boundary is the source of the dimensions and avoids duplicating that
re-association on each collector hop.

## Verification

The regression test serializes one flush containing `llm_usage`, `node_exit`,
and `log`, then asserts every generated OTLP resource group has exactly the
same `event_name` as all of its records. The runbook now provides the matching
post-rollout Loki canary.
