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
the loop. This router is the only way alerts enter the store: no other process writes
`alerts` or pushes an alert to IM (`tests/scripts/test_alert_single_path.py`);
every other condition is an event plus a Grafana rule
([decision](../../../docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md)).
One row per episode, with another IM when severity increases. The store/IM core
lives in `base/telemetry/alerts/__init__.py`. Three HTTP surfaces plus one background
reconciler:

- `POST /api/alerts` — the webhook (Grafana embedded-Alertmanager contact
  point, `deploy/lgtm/config/grafana/provisioning/alerting/contact.yml`). Upserts each alert instance
  into `alerts`, publishes it on the SSE channel, and fans firing/recovery
  IM notifications out via the im_bridge daemon, one message per rule and status in the POST
  (a count plus the first instances' summaries; a lone instance keeps the single-alert format) — every severity pushes
  (critical/warning/error, no gate).
- `GET /api/alerts` — the alert section's unresolved-first history list +
  unresolved count for the top-bar badge.
- `GET /api/alerts/stream` — SSE tail (channel `ava:alerts`, broadcast) of
  every ingest; the UI's initial fetch covers rows ingested before the
  subscription opened.
- Grafana reconciliation — on events-maintenance service start and every five minutes, fetch
  Grafana's current Alertmanager instances and resolve stored rows
  absent from that truth set. This closes the lost-RESOLVE-webhook gap.

## Store — `alerts`

One row = one alert instance, deduped by **(fingerprint, starts_at)**
(unique constraint, migration `20260813T042527_alerts`). Alertmanager may
re-send the same instance while firing and sends it once more on resolution;
the upsert updates the row instead of duplicating it.
Columns: `status` (unresolved|resolved — no ack state) /
`severity` (critical|warning|error, read from the rule's `severity` label,
normalized — anything else defaults to warning) / `alertname` / `labels` /
`annotations` (jsonb, the Alertmanager shape) / `starts_at` / `ends_at` /
`fingerprint` (Alertmanager-standard fnv-1a over sorted labels, computed
when the payload omits it) / `generator_url` / `source` (`grafana`; rows
written before 2026-10-04 also carry the retired in-process writers' tags) /
`notified_at` / timestamps.
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
`X-Alerts-Token` == the webhook token
(`AVA_ALERTS_WEBHOOK_TOKEN` — constant-time), or a cluster Bearer (the human secret or the active write generation's machine API token, which in-cluster posters present), or — only when no token is
configured — loopback trust (the single-box default: Grafana is co-located).

Response: `{processed, inserted, updated, notified}`.

### Lost-resolution reconciliation

With Grafana admin auth configured, the events-maintenance service's startup + five-minute loop resolves
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

Eligible new firing/resolved groups request durable acceptance through authenticated
`POST /send/alert-outbound-v1`; the existing IM Outbox sends the frozen available
owner subset and retains unavailable channel decisions. Acceptance is separate
from actual provider delivery; legacy `/send` remains for other immediate callers.
See [[native-alert-outbox]] for recovery, completion and rollout requirements.
Format: a severity-headed template + summary + generatorURL +
`→ <fleet UI>/insights/alerts` (recovery swaps the head for the resolved
variant). Templates live in `services/entrypoints/im_bridge/copy.py` — the single source
of user-visible IM copy (governance ruling 2026-08-08) — with zh/en variants
(the zh head carries the Chinese firing/resolved words, the en head `⚠️ ALERT [...]`); the language follows `user_settings`
`display.language` (default zh, user ruling 2026-08-13), resolved by
`base.telemetry.alerts.display_language` at ingest time. Alert labels/annotations
data is never translated. All three severities push. Recovery sends only when
the firing had been IM-notified (`notified_at` set); firing retries while
`notified_at` stays NULL. An unresolved already-notified instance re-notifies
only when severity increases (WARNING → ERROR/CRITICAL or ERROR → CRITICAL);
equal severity and downgrades stay silent. IM failures are logged, never fail the ingest.
Reconciliation repairs the durable store and SSE view but does not synthesize
an IM recovery without Grafana's resolved notification payload.

### Shadow transition facts

The ingest transaction freezes immutable revision/group/member snapshots through
`base.telemetry.alerts.shadow.AlertShadowBatch`; fingerprint gates preserve
input-order instance resolution. Repeated observations retain the original group.
Only real native SENT advances `notified_revision`; `notified_at` keeps its
first-ever timestamp. Shadow history is never proof of non-delivery and cannot
be automatically dispatched. See
[[alert-shadow-facts]] for transaction ownership and the retained history boundary.

### List

`GET /api/alerts?window=1h|6h|24h|7d&status=&severity=&limit=`
→ `{alerts: [row...], meta: {window, total, unresolved_count}}`, ordered
`(status = 'unresolved') DESC, starts_at DESC`. `unresolved_count` backs the
top-bar badge (same window/severity scope, 0 when scoped to resolved). GET and
stream sit behind the normal session/Bearer middleware (the UI + SDK).
