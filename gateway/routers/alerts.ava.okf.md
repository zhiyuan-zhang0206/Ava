---
type: doc
title: Alerts Router
description: "POST /api/alerts + GET /api/alerts + GET /api/alerts/stream — the system→human alert store (alerts), Grafana-truth resolution reconciliation, the alert-section history list, the SSE live tail, and the IM notification fan-out (Task #1224)."
tags:
- gateway
- alerts
---

# Alerts Router

The Alert system (user design 2026-08-12): Alert is fully separate from
Notice — own table, own UI section, own IM channel; nothing here touches
`agent_notices`. Grafana's embedded Alertmanager evaluates the alert rules
(`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` as code) and delivers the Alertmanager
standard webhook payload to the gateway; this router is the other half of
the loop. The cluster health probe (`cli/commands/_health_alerts.py`) posts
its edge-triggered health alerts through the same endpoint with
`source="health-probe"`, and the heartbeat liveness pass
(`services/heartbeat/liveness.py`) writes its machine offline/online edges
straight to the table (`source="machine-probe"`) — every producer rides one
store/IM pipeline: one row + one IM notification per delivered transition. The
store/IM core lives in `shared/alerts.py` (this router is one caller; the
probes run the same functions locally). Three HTTP surfaces plus one background
reconciler:

- `POST /api/alerts` — the webhook (Grafana embedded-Alertmanager contact
  point, `deploy/lgtm/config/grafana/provisioning/alerting/contact.yml`). Upserts each alert instance
  into `alerts`, publishes it on the SSE channel, and fans firing/recovery
  IM notifications out via the im_bridge daemon — every severity pushes
  (critical/warning/error, no gate).
- `GET /api/alerts` — the alert section's unresolved-first history list +
  unresolved count for the top-bar badge.
- `GET /api/alerts/stream` — SSE tail (channel `ava:alerts`, broadcast) of
  every ingest; the UI's initial fetch covers rows ingested before the
  subscription opened.
- Grafana reconciliation — on gateway startup and every five minutes, fetch
  Grafana's current Alertmanager instances and resolve stored Grafana rows
  absent from that truth set. This closes the lost-RESOLVE-webhook gap.

## Store — `alerts`

One row = one alert instance, deduped by **(fingerprint, starts_at)**
(unique constraint, migration `20260813T042527_alerts`). Alertmanager may
re-send the same instance while firing and sends it once more on resolution;
the upsert updates the row instead of duplicating it.
Columns: `status` (unresolved|resolved — no ack, no escalation) /
`severity` (critical|warning|error, read from the rule's `severity` label,
normalized — anything else defaults to warning) / `alertname` / `labels` /
`annotations` (jsonb, the Alertmanager shape) / `starts_at` / `ends_at` /
`fingerprint` (Alertmanager-standard fnv-1a over sorted labels, computed
when a direct writer omits it) / `generator_url` / `source`
(`grafana` | `health-probe` | `machine-probe`) / `notified_at` / timestamps.
Index: `(status, starts_at DESC)` serves the list path.

## Contract

### Ingest (Alertmanager webhook)

Body = the Alertmanager standard webhook payload: `{status, alerts: [{
status, labels, annotations, startsAt, endsAt, fingerprint, values,
generatorURL}]}` — the full v4 envelope (version/groupKey/receiver/
commonLabels/… ) and the slimmer Grafana-managed shape are both accepted
(extra fields tolerated; a missing per-alert status falls back to the
top-level one). Webhook `firing` maps to store `unresolved`. Alertmanager's
zero time (`0001-01-01T00:00:00Z`) in `endsAt` is stored as NULL.

Auth — the webhook cannot hold the cluster secret, so the ingest path
bypasses the session/bearer middleware and authenticates itself:
`X-Alerts-Token` (or the legacy `X-Ops-Alerts-Token`) == the webhook token
(`AVA_ALERTS_WEBHOOK_TOKEN`, legacy `AVA_OPS_ALERTS_WEBHOOK_TOKEN` accepted
— constant-time), or cluster-secret Bearer, or — only when no token is
configured — loopback trust (the single-box default: Grafana is co-located).

Response: `{processed, inserted, updated, notified}`.

### Lost-resolution reconciliation

With Grafana admin auth configured, a startup + five-minute task resolves
stored Grafana instances absent from Grafana's current Alertmanager truth.
Exact identity, race boundaries, failure posture, and the rejected timestamp
sweep: [[alert-reconciliation.ava.okf.md]].

### SSE stream

Every ingested row is published to the Redis channel `ava:alerts` as one
`AlertRow` JSON frame; `GET /api/alerts/stream` forwards the channel in
broadcast mode through the same `event_stream` machinery as agent events
(heartbeat + error frames, reconnection-safe). A Redis outage never fails
the ingest — the publish is best-effort and the initial fetch covers the
gap.

### IM notification

Firing/resolved transitions fan out through the local im_bridge daemon's
`POST /send` RPC (health port, Bearer = cluster secret, body
`{"text", "type": "alert"}`): `IMBridgeCore.notify_user` calls each loaded
adapter's `send_to_owner` (Telegram owner chat / WeChat account user /
Feishu's last p2p sender; a channel without a resolvable chat is skipped).
Format: a severity-headed template + summary + generatorURL +
`→ <fleet UI>/insights/alerts` (recovery swaps the head for the resolved
variant). Templates live in `services/im_bridge/copy.py` — the single source
of user-visible IM copy (governance ruling 2026-08-08) — with zh/en variants
(`⚠️ 告警 [...]` / `⚠️ ALERT [...]`); the language follows `user_settings`
`display.language` (default zh, user ruling 2026-08-13), resolved by
`shared.alerts.display_language` at ingest time. Alert labels/annotations
data is never translated. All three severities push. Recovery sends only when
the firing had been IM-notified (`notified_at` set); firing retries while
`notified_at` stays NULL. IM failures are logged, never fail the ingest.
Reconciliation repairs the durable store and SSE view but does not synthesize
an IM recovery without Grafana's resolved notification payload.

### List

`GET /api/alerts?window=1h|6h|24h|7d&status=&severity=&limit=`
→ `{alerts: [row...], meta: {window, total, unresolved_count}}`, ordered
`(status = 'unresolved') DESC, starts_at DESC`. `unresolved_count` backs the
top-bar badge (same window/severity scope, 0 when scoped to resolved). GET and
stream sit behind the normal session/Bearer middleware (the UI + SDK).
