---
type: doc
title: Events Maintenance — per-loop progress health
description: Each maintenance loop (dispatch, class resolution) owns a progress tracker with a hard deadline; a timed-out worker permanently wedges its tracker and makes the aggregate /healthz return 503 while its sibling loop stays healthy.
tags: []
---

# Events Maintenance — per-loop progress health

- **Per-loop progress health**: dispatch and class resolution each own a progress tracker with a hard deadline (`AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S` and `AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S`; defaults 1500s and 600s). Only completed bounded work units and bounded inter-run sleeps beat; a timed-out worker permanently wedges its tracker, parks without retrying, and makes the aggregate `/healthz` return 503 even while the sibling loop remains healthy. The payload exposes each loop's progress age, last success, last error, and wedge state (via the shared health envelope components and the `loops` snapshot) so the watchdog restart is attributable. The checkpoint trim health component was removed with the retired opt-in on 2026-09-30.

Parent: [[services/docs/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]].
