---
type: doc
title: Observed Inspector Metrics
description: Compact typed persistence of Inspector statistics from the live projection.
tags:
- observability
---

# Observed Inspector Metrics

The emitter also projects compact typed facts and day sums into Postgres. A projection write that fails is not retried or rebuilt: the event stays in `telemetry_events` but its observation is never written, so Inspector statistics are partial by that amount. `node_exit` is not stored in `telemetry_events`, so activity observations come from the live projection only. Inspector reads only Postgres; older full-day cost/turn ledgers remain disjoint historical evidence.

The gateway read boundary is [[gateway/inspect/docs/inspect.meiva.okf.md]].
The producer is `base/telemetry/metrics/observed_metrics.py`; it commits fact identities and
additive daily sums together. The emitter isolates projection failure after
writing its local mirror, without creating recursive telemetry.

Collection is not lossless: an observation the live projection failed to write stays missing.
A populated downgrade
refuses before removing any table or trigger because finite source retention may
leave these facts as the only surviving evidence.
