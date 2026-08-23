# Frontend inspector audit fixes

The frontend audit batch treated a selected time window as a data-identity
boundary, not a presentation hint. Statistics cards stop rendering a prior
window's numbers while the replacement is pending; dimming those values was
rejected because the original incident was a user trusting a visible number
under the newly selected label.

The inspector was split at its latency boundary. Current shells, liveness,
configuration, notices, and heartbeat state come from an uncached lightweight
read, while cost, activity, and throughput remain on the bounded historical
aggregate path. Keeping one response was rejected because every agent switch
would continue coupling cheap control-plane state to the Loki fan-out. The two
frontend queries retain explicit agent and window identity guards and render
section-level skeletons independently.

Sidebar intent prefetch is allowed only while the inspector is already open.
Hover waits briefly and is abortable; selection starts the same keyed reads
immediately. Prefetch while closed was rejected so the existing zero-inspect-
traffic contract remains true for users who do not use the panel.

Committed `sdk_calls` metadata is authoritative even when it is an empty array;
regex scanning is limited to streaming items where the field is absent. Alerts
history now pins the 24-hour window named by its empty-state copy, and fleet
graph counts/emptiness follow the rendered liveness-filtered graph.

The Ava Ops dashboard required no change: it already opens at six hours with no
per-panel time overrides or collapsed rows. Its 32 instant range-scanning
targets are stat/table/barchart whole-window totals, where bucketed range
queries would change the displayed value. The retired metrics redirect also
required no change because its probe, cancellation guard, timer cleanup, and
unmount regression already cover the reported concern.
