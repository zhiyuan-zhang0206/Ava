# Fleet graph frozen-source cache

The fleet graph caches its two immutable edge inputs in Redis for 24 hours:
the raw Postgres events archive together with its freeze boundary, and the
legacy Loki interval between that boundary and the index-label cutover. The
indexed Loki tail and Postgres node rows remain live reads.

This keeps weighting and request-specific filtering out of the cache. Archive
and Loki rows enter one merge path, where the selected time window, live-agent
frontier, decay constant, and final weight cutoff are applied per request.
Versioned cache keys make a future payload-shape change an explicit rollover.

Caching was chosen because both slow intervals are historical facts, while
re-scanning them on each poll consumed most of the endpoint's cold-path time.
A process-only cache would not be shared across gateway workers or restarts.
The accepted tradeoff is that a collector retry delivered late into the legacy
Loki interval can remain hidden until the 24-hour entry expires. Redis failures
remain fail-open and fall through to the source reads.
