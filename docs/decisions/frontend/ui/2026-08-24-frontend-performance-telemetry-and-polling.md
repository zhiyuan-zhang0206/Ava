# Frontend performance telemetry and polling

The frontend performance audit established two constraints: browser-perceived
latency must be visible through the existing bounded telemetry path, and
snapshot polling must not dominate an otherwise SSE-driven page.

Performance instrumentation stays dependency-free. Native
`PerformanceObserver` entries report FCP immediately and final LCP, CLS, and
INP when the authenticated page hides. These samples bypass the interaction
dedupe window because each page load matters, but they retain the existing
per-tab rate and buffer caps. The central API fetch wrapper reports only calls
slower than 800ms, using schema-safe route keys whose numeric segments collapse
to `id`; the telemetry POST is excluded. API-base resolution moved to a neutral
module so this choke-point instrumentation does not create an import cycle.

Composer latency uses a per-agent in-memory mark from submit to the first SSE
event that activates the timeline turn state. Persistence and cross-tab
correlation were rejected: this metric describes one browser interaction, and
a 120-second sanity bound discards stale marks.

The always-mounted system-status observer, the visible Insights status view,
and expanded schedule logs now share a 15-second cadence. The 5-second cluster
update watchdog and 3-second interactive shell capture remain unchanged because
their shorter intervals protect deliberate recovery and terminal-use cases.
