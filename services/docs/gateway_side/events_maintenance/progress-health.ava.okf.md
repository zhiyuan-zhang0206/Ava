---
type: doc
title: Events Maintenance — per-loop progress health
description: Each maintenance loop (dispatch, class resolution) owns a progress tracker with a hard deadline; a timed-out worker permanently wedges its tracker and makes the aggregate /healthz return 503 while its sibling loop stays healthy.
tags: []
---

# Events Maintenance — per-loop progress health

- **Per-loop progress health**: dispatch and class resolution each own a progress tracker with a hard deadline (`AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S` and `AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S`; defaults 1500s and 600s). Only completed bounded work units and bounded inter-run sleeps beat; a timed-out worker permanently wedges its tracker, parks without retrying, and makes the aggregate `/healthz` return 503 even while the sibling loop remains healthy. The payload exposes each loop's progress age, last success, last error, and wedge state (via the shared health envelope components and the `loops` snapshot) so the watchdog restart is attributable. The checkpoint trim health component was removed with the retired opt-in on 2026-09-30.

- **Registry gauge loop**: a third resident loop samples `max(agents.id)` once a minute and emits the `agent_registry` event the growth dashboard reads (`services/upkeep/events_maintenance/registry_gauge.py`, progress threshold 180 s). The loops share one `TaskGroup`: a loop that raises ends the process and the supervisor restarts it.

- **Pass ownership and stop**: the service's existing `TaskGroup` owns each blocking pass's async proxy, including one whose deadline expired. A completed pass returns its original exception to its loop's retry or schema-drift policy; a late failure is reported with its traceback without restoring health. Stop cancels all pass proxies before pool, health-server and pidfile cleanup. The daemon retains its existing hard exit after async cleanup, skipping executor and interpreter thread joins so the watchdog can replace a wedged worker.

- **Alert reconciliation loop**: on a unit holding `GRAFANA_ADMIN_PASSWORD`, a loop repairs lost Grafana resolution webhooks every five minutes (`services/upkeep/events_maintenance/alert_reconciler.py`, see [[gateway/alerts/docs/alert-reconciliation.ava.okf.md]]); a Grafana that is down or sends a bad snapshot is logged and retried, never fatal.

Parent: [[services/docs/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]].
