# Fleet graph freshness semantics

The FleetView graph now distinguishes an incomplete or fallback snapshot from
a fresh graph whose telemetry heartbeat is delayed. A successful source read
always refreshes both Redis caches and records its snapshot time; heartbeat lag
is reported separately so it cannot erase the last-good fallback during an
observability outage.

Loki reads retain unlabeled rows alongside the current cluster label. The
unlabeled slice is this single-cluster Loki deployment's pre-labeling history,
while rows labeled for another cluster remain excluded.

Truncated snapshots refresh the short poll cache but never replace the
last-good fallback.
