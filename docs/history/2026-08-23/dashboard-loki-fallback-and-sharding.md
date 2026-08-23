# Dashboard Loki fallback and sharding

The dashboard's four Loki aggregates now split long windows into the shared
clock-aligned spans no longer than three hours, merge their sums, and keep the
existing one-query path for shorter windows. This avoids sending 72-hour and
seven-day instant range vectors as one expensive Loki request.

Loki transport failures now become the dashboard's typed retriable 503 with
`Retry-After: 1`; the failing query shape remains emitted once by
`loki_events`. Local Loki-budget refusals keep their existing process-wide 503
handler and reason.
