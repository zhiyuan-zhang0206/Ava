---
type: doc
title: Events Maintenance — per-loop progress health
description: Each maintenance loop (dispatch, checkpoint trim, class resolution) owns a progress tracker with a hard deadline; a timed-out worker permanently wedges its tracker and makes the aggregate /healthz return 503 while sibling loops stay healthy.
tags: []
---

# Events Maintenance — per-loop progress health

- **Per-loop progress health**: dispatch, checkpoint trim, and class resolution each own a progress tracker with a hard deadline (`AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S`, `AVA_EVENTS_MAINTENANCE_TRIM_DEADLINE_S`, `AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S`; defaults 1500s / 300s / 600s). Only completed bounded work units and bounded inter-run sleeps beat; a timed-out worker permanently wedges its tracker, parks without retrying, and makes the aggregate `/healthz` return 503 even while sibling loops remain healthy. The payload exposes each loop's progress age, last success, last error, and wedge state (via the shared health envelope components and the `loops` snapshot) so the watchdog restart is attributable.

Parent: [[services/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]].
