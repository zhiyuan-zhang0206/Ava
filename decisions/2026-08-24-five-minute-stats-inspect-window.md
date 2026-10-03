# Five-minute stats and inspector window

The dashboard, fleet graph, and per-agent inspector now accept `?hours=0` as
the last five minutes. The wire field remains the existing `hours` enum rather
than adding a second duration parameter, so the frontend's persisted window
setting and every read surface share one validated contract.

The zero value is resolved centrally before Loki and fleet-edge queries run.
Fleet's Prometheus counter query receives the resulting `timedelta`, producing
the native `[5m]` range rather than an invalid zero-hour range.
