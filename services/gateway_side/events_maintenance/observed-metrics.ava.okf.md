---
type: doc
title: Observed Inspector Metrics
description: Compact typed persistence and bounded background recovery for Inspector statistics.
tags:
- observability
---

# Observed Inspector Metrics

the emitter also projects compact typed facts and day sums into Postgres. The maintenance pass idempotently recovers retained Loki plus local full/rollup JSONL with atomic byte cursors. The 120-second pass reserves half its budget for independent JSONL recovery. Source scans are recovery evidence, never completeness watermarks. The frozen archive owns timestamps through its last row inclusive; JSONL cursors count excluded archive-era rows and continue, preserving disjoint source ownership. Inspector reads only Postgres; older full-day cost/turn ledgers remain disjoint historical evidence. Runner mirrors can be replayed with `python -m services.events_maintenance.observed_metrics --jsonl PATH`; archived Loki recovery uses `--archive`. The original sources remain unchanged.

The gateway read boundary is [[gateway/routers/agent-inspect.ava.okf.md]].
The producer is `shared/metrics/observed_metrics.py`; it commits fact identities and
additive daily sums together. The emitter isolates projection failure after
writing its local mirror, without creating recursive telemetry.

Recovery scans record source traversal, not lossless collection. Missing upstream
observations can remain unknown after a successful scan. A populated downgrade
refuses before removing any table or trigger because finite source retention may
leave these facts as the only surviving evidence.
