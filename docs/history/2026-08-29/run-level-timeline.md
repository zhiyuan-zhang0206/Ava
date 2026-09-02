# Run-level timeline uses the event stream as its durable skeleton

## Context

The tracing surface needs a shareable run-to-turn visualization for an agent
without making its availability depend on either of the observed root-span
shapes. A per-turn root and a long-lived session root both emit the same Loki
events, while Tempo remains the appropriate optional source for later
call-level drill-down.

## Decision

`GET /api/agents/{id}/run-timeline` derives the run skeleton solely from the
bounded Loki event history. A `turn_end` row supplies a turn's wall-clock
interval and joins its one-to-one `llm_usage` measurement by `span_id`.
Execution, lifecycle, compact, idle, and failure events decorate those rows
instead of introducing a new collection path.

The default window is the latest observable lifecycle start through the latest
compact in Loki retention. When no compact exists it ends at the current time;
when lifecycle history has aged out it is a bounded last-24-hours view. The
event stream has no durable standalone `initialize context` marker, so the
first completed turn following that lifecycle boundary is the response's
explicit initialize-row anchor.

The UI keeps time and token quantities as separate panels. It uses absolute
`in_total` values (which include cache reads) as the primary token measure,
with output highlighted separately, and renders dashed ordinal correspondence
markers rather than implying a shared numeric scale.

## Consequences

The route is read-only, has no new collection dependency, and works for both
trace root shapes. Long runs can request server-side time buckets; the page
switches to one-hour buckets when the initial view exceeds 400 turns. Tempo
call-level and checkpoint-content drill-down remain follow-up work.

## Update — resilient event association and bounded run views

Execution events are associated exactly once by their completed-turn time
window, never by `trace_id`: a session-root trace can span many turns. Usage
keeps the exact `span_id` join when present, then associates unjoined historical
usage once by the same bounded time-window rule. The response distinguishes
turns recovered by that fallback from turns with no usage at all, so the UI can
make incomplete tracing visible instead of presenting a silent zero-token view.

The compact-ended session remains the default required session route. The
`session=current` selector exposes the latest lifecycle start through now, and
the compact view reports post-boundary activity with a direct switch. The page
requests one-hour buckets up front for server-selected or six-hour-and-larger
windows, while preserving turn detail for narrower explicit windows.

Superseded for the frontend presentation and default request by
[`decisions/2026-09-02-run-timeline-interactive-turn-track.md`](../../../decisions/2026-09-02-run-timeline-interactive-turn-track.md).
