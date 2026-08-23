# Ava Ops dashboard overhaul

The unified Ava Ops dashboard now treats every Loki target's `legendFormat`
as part of the panel contract. Static names identify aggregate series, and
label templates identify grouped series; legacy `byName` display-name
overrides are absent because they do not reliably match Loki output.

The core Statistics surface covers the full popover, including distinct input
and output token totals, cache-hit percentage, and successful-turn duration.
The six daily total tiles pin `timeFrom` to 24 hours, independent of the
dashboard-wide selector. Trend panels use fixed semantic buckets while Fleet
totals remain whole-window grouped instant queries.

Core and plugin metric registrations are locked to the hand-maintained
dashboard JSON. The registry now permits the narrow `telemetry|log` selector
used by unresolved-event totals because historical resolution records span
those two categories; other untemplated event/category filters remain
rejected.

The execution child now warms enabled OTLP export before agent code runs and
flushes it after clean completion, restoring short-lived SDK-call exports
(timed-out or cancelled children skip the flush so teardown timing is unchanged).
