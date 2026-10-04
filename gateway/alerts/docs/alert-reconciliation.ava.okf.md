---
type: doc
title: Alert Resolution Reconciliation
description: "Startup + periodic comparison of unresolved Grafana alert rows with Grafana's active Alertmanager instance truth."
tags:
- gateway
- alerts
---

# Alert Resolution Reconciliation

When `GRAFANA_ADMIN_PASSWORD` is set, the events-maintenance service
(`services/events_maintenance/alert_reconciler.py`, one of its resident loops)
reconciles at start and every five minutes. It reads
`GET /api/alertmanager/grafana/api/v2/alerts` from the co-located Grafana with
admin basic auth and compares exact `(fingerprint, startsAt)` instance keys to
stored `status='unresolved'` rows. Fingerprint alone is not
enough: separate episodes of one rule reuse it.

An absent stored instance becomes `resolved`, gets `ends_at`/`updated_at` set
to the observation time and `annotations.reconciliation = "reconciled: no
longer firing"`, then publishes the same resolved `AlertRow` SSE frame as a
webhook resolution. Every row belongs to Grafana's evaluator, so the sweep is
source-blind: a row a retired in-process writer (`health-probe`, `machine-probe`)
left open is absent from Grafana's set too and closes with the rest.

The snapshot start time is a write boundary: rows ingested or refreshed after
the Grafana read begins are ineligible for that pass, preventing a concurrent
new firing from being resolved against an older upstream snapshot. Any auth,
network, response-validation, or unreachable-database failure leaves every row
unchanged and retries on the next pass; an unexpected error ends the loop and the
service restarts.

A timestamp-only 2h sweep is deliberately not used. The rule groups evaluate
every 1m/5m, but the notification policy's unchanged-firing `repeat_interval`
is 4h; webhook silence for 2h therefore does not prove an alert resolved.
