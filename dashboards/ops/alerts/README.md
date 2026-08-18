# dashboards/ops/alerts — Ava ops alert rules (as code)

Grafana Alerting rules for the Ava event system, provisioned as code. Since
the LGTM cutover (Task #1224) they evaluate against the **LGTM read side**:
R1-R3, R5-R7 query **Loki** (every event is one OTLP log line under
`{service_name="unknown_service"}`, body = the full event JSON, so `| json`
flattens each line to labels — the OTel-detected labels are structured
metadata and cannot be matched by `{…}` selectors), and R4 queries
**Prometheus** (the `ava_llm_usage_latency_ms` histogram, the OTLP metric
mirror of the `llm_usage` event). The retired Postgres events read path
(#1197) is gone — nothing queries the `ops` datasource from these rules.

Datasources are provisioned beside this file (`datasources.yml`, uids
`loki` / `prometheus`; the `ops` Postgres datasource stays for the ava-ops
dashboards).

## Where these run (LGTM stack)

Since the single-Grafana merge (Task #1264) the LGTM Grafana container
(3003, deploy/local/lgtm) evaluates these rules: `rules.yml` and
`contact.yml` are copied to
`deploy/local/lgtm/config/grafana/provisioning/alerting/` (mounted into the
container). The copy must stay in sync — guarded by
`tests/test_lint_lgtm_alerting_sync.py`; the only allowed divergence is the
contact point's webhook URL (`host.docker.internal` inside the container).
The 3002-era provisioning copy (`~/.ava/grafana/provisioning/alerting/`,
synced by `_sync_grafana_provisioning`) is retired with 3002.

## Rules (7, 2026-08-12)

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `ava-ops-warning-error-spike` | WARNING+ERROR+CRITICAL spike | 5m count > 3× prior-15m rate AND ≥ 30 (Loki) | 5m | error |
| `ava-ops-sse-drop-backlog` | sse_drop + event_log_drop backlog | SUM(payload.n) in 10m > 100 (Loki unwrap) | 5m | warning |
| `ava-ops-agent-restart-spike` | agent_restarted spike | count in 15m > 30 (Loki) | 5m | warning |
| `ava-ops-llm-latency-p95` | llm_usage latency p95 | histogram p95 in 10m > 60000 ms (Prometheus) | 10m | error |
| `ava-ops-delivery-stalled-backlog` | delivery_stalled fresh backlog | fresh (age_s<600) count in 10m > 50 (Loki) | 5m | warning |
| `ava-ops-events-freshness` | event stream stalled | no events in Loki for 5m (absent_over_time) | 5m | error |
| `ava-ops-trace-disk-watermark` | trace recording auto-degraded | recording_disabled_disk_watermark count in 24h > 0 (Loki) | 5m | error |

Severity follows the alert-system vocabulary (Task #1224):
critical/warning/error — all three push to IM, no gate. Thresholds are
calibrated against live data (2026-08-04, see header comment in
`rules.yml`). R7 (trace-disk-watermark) is a chronic-condition alert: any
degradation event in the trailing 24h keeps it firing, so one episode = one
onset + one recovery IM instead of one per event.

### Migration notes (Postgres → LGTM, 2026-08-12)

- **R1/R2/R3/R5/R7** — same semantics, Loki `count_over_time` /
  `sum_over_time` + `| json` filters. R2 unwraps `attributes_n` (SUM of the
  real per-row drop counts, the same contract the Ops panel reads); R5's
  freshness discriminator is the numeric comparison
  `| attributes_age_s < 600` on the flattened label.
- **R4** — p95 now comes from `histogram_quantile(0.95,
  sum by (le) (rate(ava_llm_usage_latency_milliseconds_bucket[10m])))`,
  an approximation of the exact SQL percentile (the same read the Insights
  Ops panel uses). Absent samples → NoData → OK (no spurious night fires).
- **R6** — semantics changed with the stream: the old rule measured the
  retired events TABLE's write freshness; the new rule probes the live
  stream with `absent_over_time({service_name="unknown_service"}[5m])` —
  fires when no event of ANY kind reached Loki in 5m (emitter stall or a
  broken OTLP export path alike — the 2026-08-12 preflight-bug outage
  class), at the cost of no longer distinguishing the two. When the stream
  flows the query returns no data (NoData → OK).
- **R4/R6 data-source caveat** — every rule reads Loki/Prometheus now; if
  the OTLP export path fails (as it did 2026-08-12), new events stop
  landing and R1-R5, R7 go quiet with them — R6 exists exactly to scream
  about that outage class, and the health-probe chain covers the rest.

## Sync to the live Grafana

The running Grafana (on the gateway host, `127.0.0.1:3002`, launchd
`com.ava.grafana`) reads alert-rule provisioning from
`~/.ava/grafana/provisioning/alerting/`. `_update_local.py` copies
`rules.yml` + `contact.yml` (alerting/) and `datasources.yml`
(datasources/) there on every rollout and restarts Grafana
(`launchctl kickstart -k gui/$(id -u)/com.ava.grafana`) — alert-rule
provisioning does **not** hot-reload file changes (verified 2026-08-04);
datasource and contact-point provisioning do.

Requires Grafana Unified Alerting enabled — `[unified_alerting] enabled =
true` in `~/.ava/grafana/grafana.ini` (flipped on 2026-08-04 by the W3 PR).

## How to add a rule

1. Add a rule to `rules.yml` (uid must be unique; keep `folder: Ava`, group
   `ava-ops`, `interval: 1m`). Loki queries: stream selector
   `{service_name="unknown_service"}` + `| json` before any field filter,
   and wrap every count in `sum(...)` — the unknown_service family has >500
   streams over a day and an unaggregated count hits Loki's per-query
   series cap.
2. Copy to `~/.ava/grafana/provisioning/alerting/rules.yml` (or wait for
   the next rollout's sync).
3. Verify via admin API: `GET /api/v1/provisioning/alert-rules` (password in
   `~/.ava/grafana/admin_password`). Synthetic events are no longer SQL
   inserts — push a test event through the emitter (any Ava process emits
   on activity; e.g. trigger a real event or use the Loki push API), then
   watch `GET /api/prometheus/grafana/api/v1/rules` for the state
   transition.

## Notifications — wired through the alerts ingest

Rules evaluate in Grafana and POST firing/resolved webhooks to the gateway's
`POST /api/alerts` (provisioned contact point + policy in `contact.yml`).
The ingest stores each alert instance in `alerts` (deduped by fingerprint ×
starts_at, Alertmanager webhook shape) and fans firing notifications out
through the im_bridge daemon (the only sanctioned IM surface) — one alert
instance = one row + one IM, every severity (critical/warning/error, no
gate). The cluster health probe posts through the same endpoint
(`source=health-probe`); the heartbeat liveness pass writes machine
offline/online edges straight to the table (`source=machine-probe`).

## Events-stream cutover (task #1197, done)

The unified emitter ships every event as an OTLP log (Loki) and every
telemetry event's numeric payload fields as OTLP metrics (Prometheus); the
Postgres `events` write was retired with the cutover and the JSONL mirror
stays as the local copy. These rules read the LGTM side exclusively.
