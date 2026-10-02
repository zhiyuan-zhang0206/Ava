---
type: doc
title: Observed Inspector Metrics
description: Compact typed persistence and bounded background recovery for Inspector statistics.
tags:
- observability
---

# Observed Inspector Metrics

the emitter also projects compact typed facts and day sums into Postgres. Each maintenance pass recovers the observations the projection missed from `telemetry_events` (`services/events_maintenance/observed_metrics.py`): the last seven days of the supported event families whose observation is absent, written through the same `observe_row` reduction, so a recovered row and a live one are the same observation (the table's signed `event_uid` is mapped to the observation's unsigned event id). The pass pages by `(ts, event_uid)` under a 120-second budget, and a row that cannot become an observation is skipped. Scans are recovery evidence, never completeness watermarks. The frozen archive owns timestamps through its last row inclusive, so rows at or before it are left alone. Inspector reads only Postgres; older full-day cost/turn ledgers remain disjoint historical evidence.

The gateway read boundary is [[gateway/inspect/docs/inspect.ava.okf.md]].
The producer is `base/telemetry/metrics/observed_metrics.py`; it commits fact identities and
additive daily sums together. The emitter isolates projection failure after
writing its local mirror, without creating recursive telemetry.

Recovery scans record source traversal, not lossless collection. Missing upstream
observations can remain unknown after a successful scan. A populated downgrade
refuses before removing any table or trigger because finite source retention may
leave these facts as the only surviving evidence.
