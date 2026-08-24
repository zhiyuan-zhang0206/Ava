# Grafana alerting — Ava ops alert rules (as code)

Grafana Alerting rules for the Ava event system, provisioned as code. Since
the LGTM cutover (Task #1224) they evaluate against the **LGTM read side**:
R1-R3, R5-R7 and R13 query **Loki** (every event is one OTLP log line under
`{service_name="unknown_service"}`, body = the full event JSON, so `| json`
flattens each line to labels), while R4 and the gateway-metrics silence rule
query **Prometheus** (the `ava_llm_usage_latency_milliseconds` histogram and
the `ava_gateway_latency_count_total` heartbeat). The retired Postgres events
read path (#1197) is gone — nothing queries the `ops` datasource from these
rules.

Datasources are provisioned beside this file (`datasources.yml`, uids
`loki` / `prometheus`; the `ops` Postgres datasource stays for the ava-ops
dashboards).

### LogQL migration timeline

- Until pre-cutover chunks expire, every rule keeps the legacy selector
  `{service_name="unknown_service"}` plus `| json`, because old chunks lack
  the promoted `event_name` / `agent_id` stream labels.
- After `LEGACY_READ_EXPIRES_AT` (2026-08-30T11:10Z), tracked task #1467 moves
  `event_name` / `agent_id` filters into the stream selector while retaining
  `| json` for level, category, and attributes filters.
- Never promote those filters before legacy chunks expire: doing so silently
  drops seven days of history.

## Where these run (LGTM stack)

Native Grafana on the LGTM host (port 3003) evaluates these rules from the
source checkout's `deploy/lgtm/config/grafana/provisioning/alerting`
directory. The contact point posts to `127.0.0.1:8000`, reaching the gateway
on the same host loopback.

## Rules (20)

The rules are split between `ava-ops` (15 rules, evaluated every minute:
R1-R6, the gateway-metrics silence rule, R8-R12, and R14-R16) and
`ava-ops-slow` (five rules, evaluated every five minutes: R7, R13, R17's two
route tiers, and R18). Their existing `for` windows remain unchanged.

Application layer — the Loki event stream plus the LLM latency histogram:

| uid | Group | Metric | Condition | `for` | Severity |
|-----|-------|--------|-----------|-------|----------|
| `ava-ops-warning-error-spike` | `ava-ops` | WARNING+ERROR+CRITICAL spike | 5m count > 3× prior-15m rate AND ≥ 30 (Loki) | 5m | error |
| `ava-ops-sse-drop-backlog` | `ava-ops` | sse_drop + event_log_drop backlog | SUM(payload.n) in 10m > 100 (Loki unwrap) | 5m | warning |
| `ava-ops-agent-restart-spike` | `ava-ops` | agent_restarted spike | count in 15m > 30 (Loki) | 5m | warning |
| `ava-ops-llm-latency-p95` | `ava-ops` | llm_usage latency p95 | histogram p95 in 10m > 60000 ms (Prometheus) | 10m | error |
| `ava-ops-delivery-stalled-backlog` | `ava-ops` | delivery_stalled fresh backlog | fresh (age_s<600) count in 10m > 50 (Loki) | 5m | warning |
| `ava-ops-events-freshness` | `ava-ops` | event stream stalled | no events in Loki for 5m (absent_over_time) | 5m | error |
| `ava-ops-gateway-metrics-silent` | `ava-ops` | gateway_latency heartbeat | no samples in Prometheus for 5m (absent_over_time) | 5m | error |
| `ava-ops-trace-disk-watermark` | `ava-ops-slow` | trace recording auto-degraded | recording_disabled_disk_watermark count in 24h > 0 (Loki) | 5m | error |
| `ava-ops-llm-billing-quota` | `ava-ops-slow` | LLM key out of credit / quota | llm_provider_error with billing=true in 15m > 0 (Loki) | 0m | critical |
| `ava-ops-gateway-latency-route-warning` | `ava-ops-slow` | Gateway latency: fast route p95 | p95 > 3s for 5m (Loki, LLM-bound + inherently-slow routes excluded) | 5m | warning |
| `ava-ops-gateway-latency-route-error` | `ava-ops-slow` | Gateway latency: fast route p95 | p95 > 10s for 5m (same route filter) | 5m | error |
| `ava-ops-turn-duration-p95` | `ava-ops-slow` | Turn duration p95 (collective slowdown) | histogram p95 > 75s for 10m (Prometheus, 24h baseline 37.6s × 2) | 10m | warning |

Infrastructure layer (issue #46) — the per-machine OTel Collector sidecar's
own scrapes, labelled `host` (OS hostname / physical identity) and
`machine_name` (Ava roster identity). Per the 2026-08-24 user ruling, alerts
group by `machine_name`, so win and wsl remain separate series. These
thresholds are deployment facts, not framework constants — what counts as
"too much CPU" depends on box specs and co-tenancy:

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `ava-ops-host-cpu-saturated` | non-idle CPU | avg by machine_name > 0.90 (Prometheus) | 15m | warning |
| `ava-ops-host-memory-pressure` | memory utilization | avg by machine_name > 0.90 (Prometheus) | 15m | warning |
| `ava-ops-host-disk-watermark` | filesystem utilization | max by machine_name+mountpoint > 0.90 (Prometheus) | 15m | warning |
| `ava-ops-pg-connection-saturation` | Postgres backends vs max | ratio > 0.80 (Prometheus) | 15m | warning |
| `ava-ops-redis-memory` | Redis resident set | > 2 GiB (Prometheus) | 15m | warning |

Collector delivery layer — each sidecar scrapes its per-unit loopback
self-metrics endpoint (`AVA_OTELCOL_METRICS_PORT`, default 8888) and relays it
through `metrics/infra`. Prometheus's OTLP translation adds `_total` to the raw
monotonic counters:

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `ava-ops-otelcol-queue-pressure` | exporter queue size/capacity | current ratio > 0.80 per machine_name+exporter+signal | 5m | error |
| `ava-ops-otelcol-enqueue-failures` | new enqueue rejections | `increase(otelcol_exporter_enqueue_failed_*_total[5m]) > 0` | 0m | error |
| `ava-ops-otelcol-host-silent` | recently-seen collector absent | machine seen in 24h has no `otelcol_process_uptime_total` in 5m | 0m | error |

The queue rule uses current gauges so it resolves after recovery; the reject
rule uses a bounded counter delta so one historical drop does not keep
alerting until the process restarts — it resolves after a clean 5-minute
window. The silence query's 5-minute absence window is already its
debounce, hence no second `for` delay. Its 24-hour historical machine set expires
retired machines naturally; the fleet heartbeat owns permanent membership.

The three slow-request rules (R17/R18, 2026-08-23, task #1399) close the
user-visible-latency gap: fast-route p95 thresholds calibrated against 7d
route data (the emitter's single route-classification source is
`gateway/_latency.py`), and the turn-duration rule catches fleet-wide slowdown
(p95 vs the 24h baseline ×2) rather than single long turns. All three carry
`notify_im: "false"` — the PM slow-request convention is warning-first and
no IM fan-out (alert-fatigue ruling 2026-08-22); the gateway honors the
label once the IM gating PR (#3219) lands, until then they reach IM like
the rest.

Severity follows the alert-system vocabulary (Task #1224):
critical/warning/error — all three push to IM, no gate. Thresholds are
calibrated against live data (2026-08-04, see header comment in
`rules.yml`). R7 (trace-disk-watermark) is a chronic-condition alert: any
degradation event in the trailing 24h keeps it firing, so one episode = one
onset + one recovery IM instead of one per event.

R13 (llm-billing-quota) is the one rule with no threshold and no `for`
window: an out-of-credit API key fails every turn in the fleet and only a
human spending money clears it, so the first rejection is already the whole
incident. Its discriminator is the `billing` field the emitter writes from
`shared/lm/errors.py`'s cross-provider predicate (HTTP 402 plus a per-vendor
vocabulary matched against the response body's `error.type` AND `error.code`) —
a new provider is covered by adding its string there, with no edit to
`rules.yml`.

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
  landing and R1-R5, R7, R13 go quiet with them — R6 exists exactly to scream
  about that outage class, and the health-probe chain covers the rest.

## Sync to the live Grafana

There is no copy step: native Grafana reads this directory from the source
checkout. Alert-rule provisioning does **not** hot-reload file changes
(verified 2026-08-04), so restart it after editing `rules.yml` with
`launchctl kickstart -k gui/$(id -u)/com.ava.grafana.<home-slug>` (first run
`launchctl bootstrap gui/$(id -u) <plist>` if the job is not loaded).
Datasource and contact-point provisioning do hot-reload.

## How to add a rule

1. Add a rule to `rules.yml` (uid must be unique; keep `folder: Ava`). Put fast
   rules in `ava-ops` (`interval: 1m`) and expensive slow-request rules in
   `ava-ops-slow` (`interval: 5m`). Loki queries: stream selector
   `{service_name="unknown_service"}` + `| json` before any field filter,
   and wrap every count in `sum(...)` — the unknown_service family has >500
   streams over a day and an unaggregated count hits Loki's per-query
   series cap.
2. Land the change; on the LGTM host restart native Grafana
   (rule provisioning does not hot-reload).
3. Verify via native Grafana's admin API:
   `GET /api/v1/provisioning/alert-rules`. Synthetic events are no longer SQL
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
