# Local LGTM trace viewer

A read-only **Tempo + Loki + Prometheus + Grafana** stack hosted on one machine
as the viewer for Ava's telemetry. Decided 2026-08-11 (task #1170): no Langfuse
machine; the most general OTel frontend, hosted locally, viewer-only and
start/stop-able at will.

## Why this stack

| Criterion | Choice |
|---|---|
| 通用性 | Tempo 是 Grafana Labs 的 OTLP 原生 trace 后端，与 Loki/Prometheus 共用 Grafana 一个 UI |
| 成熟度 | LGTM 是当前自托管可观测性事实标准组合 |
| 本地部署 | docker compose 单机跑 5 容器；OrbStack 已装 |
| UI | Grafana 13（统一 trace/log/metric 探索，Tempo 数据源原生支持 trace 瀑布图） |

Tempo 官方不发布 macOS 二进制（仅 linux/windows），所以本栈走 docker
compose；Loki/Prometheus/Grafana 亦有原生二进制，但统一容器化便于复现。
被否决的候选：Jaeger v2（原生 macOS、CNCF 毕业，但只有 trace 没有
log/metrics，且 UI 单独一套）；SigNoz（ClickHouse 过重）；Zipkin（老、UI 弱）。

## What it runs

| Container | Image (pinned) | Ports (host) | Role |
|---|---|---|---|
| tempo | grafana/tempo:3.0.2 | 3200 query, 14318 OTLP/HTTP | trace backend (local block storage) |
| loki | grafana/loki:3.7.6 | 3100 | log backend (filesystem, 7d retention; native OTLP ingest at /otlp/v1/logs) |
| promtail | grafana/promtail:3.6.0 | — | tails `$AVA_HOME/logs/*.out.log` + updater/rollout tees into Loki |
| prometheus | prom/prometheus:v3.13.2 | 9090 | metrics (self-scrape + OTLP receiver) |
| grafana | grafana/grafana:13.1.3 | 3003 | unified UI (anonymous viewer) |

No otel-collector container since task #1266: the OTLP entry is the **native
per-machine sidecar** (`ava-otel-collector` session, supervised by the
watchdog) on host 4317/4318. It fans out traces → Tempo (host 14318 OTLP/HTTP),
logs → Loki (3100 `/otlp/v1/logs`), metrics → Prometheus (9090 OTLP receiver),
and mirrors traces to `$AVA_HOME/traces/spans.jsonl` (rotated) for local grep.
The sidecar buffers in a file-backed queue while this stack is down, so
`stop.sh` no longer loses in-flight telemetry.

Data lives in docker named volumes (`tempo-data`, `loki-data`, `prom-data`,
`grafana-data`) — `docker compose down` keeps them, `down -v` wipes them.
`restart: "no"`: the stack stays off after reboot until `start.sh` is run.

## Start / stop

```bash
bash deploy/local/lgtm/start.sh   # idempotent; auto-starts OrbStack if needed
bash deploy/local/lgtm/stop.sh    # stops; data persists
```

## Session logs in Loki (2026-08-12)

Session stdout (`$AVA_HOME/logs/*.out.log` — gateway, rollout, frontend,
`ava-agent-<id>`, `ava-schedule-<id>`, agent shells) is tailed into Loki by
promtail, plus the updater/rollout POSIX tee files (`updater-*.log`,
`rollout-*.log`). The write-side `ava logs` CLI was removed the same day —
Loki is the one query path for raw session output.

Labels:

| Label | Values |
|---|---|
| `job` | `ava-sessions` (all `*.out.log`) / `ava-orchestration` (updater+rollout tees) |
| `service` | the session name (`ava-gateway`, `ava-agent-1818`, `ava-agent-7-shell-0-uv-sync`, …) |

Not ingested (by design): `agent-<id>.log` / `agent-<id>.stderr.log` (loguru)
— that content already reaches Loki structured through the OTLP event stream
under `service_name`, so a second raw copy would only double the volume.
Note the two label namespaces: raw session logs use `service`, OTLP events
use `service_name`.

Retention: 7 days (`loki.yaml` → `limits_config.retention_period` +
`compactor.retention_enabled`), matching the write-side JSONL mirror. The
initial bring-up backfills the existing logs dir (~130MB across ~2000 files)
once; steady state adds roughly ~20MB/day.

Config changes apply with `docker compose restart loki promtail` (configs are
bind-mounted read-only; neither service reloads on its own). The prod copy of
these files lives at `~/.ava/source/deploy/local/lgtm/` — it picks the change
up on the next `ava cluster update`, and then needs the same restart. The
logs-dir bind defaults to `~/.ava/logs` (the operator's `$HOME` — no
machine-specific path is baked in); override with `AVA_LOGS_DIR=/path/to/logs
bash start.sh`.

Query paths:

```bash
# Grafana (anonymous viewer) — Explore > Loki datasource:
{job="ava-sessions"} |= "error"
{service="ava-gateway"}
{service=~"ava-agent-.+"} |~ "(?i)traceback"

# logcli (if installed on the host):
logcli --addr http://127.0.0.1:3100 query '{service="ava-gateway"}' --since=1h --limit=100
logcli --addr http://127.0.0.1:3100 query '{service="ava-agent-1818"}' --tail

# Loki HTTP API (any host):
curl -G -s http://127.0.0.1:3100/loki/api/v1/query \
  --data-urlencode 'query={service="ava-gateway"}' \
  --data-urlencode 'limit=50' \
  --data-urlencode 'time='$(date +%s)000000000
```

## Access

- Grafana: http://localhost:3003 (anonymous viewer, no login; set
  `GRAFANA_ROOT_URL` in `.env` when the UI is reached through a different
  host, e.g. a tailnet address)
  - Tempo datasource (default) — Explore > Traces: search/waterfall once the
    exporter ships traces
  - Loki datasource — Explore > Logs
  - Prometheus datasource — Explore > Metrics
- OTLP ingest: the NATIVE sidecar (ava-otel-collector session) owns
  `http://localhost:4318` — /v1/traces → Tempo (14318), /v1/logs → Loki,
  /v1/metrics → Prometheus. This is the contract of
  `AVA_TELEMETRY_OTLP_ENDPOINT` (default http://127.0.0.1:4318) for the live
  exporters; `ava trace ship` replays straight to Tempo's 14318.

## Zero-impact contract

- Nothing here writes to `~/.ava/traces` (the mirror — the sidecar does), the
  events table, or any main-flow service. The only touch on `$AVA_HOME` is promtail's read-only
  bind of `$AVA_HOME/logs` (session stdout → Loki).
- When stopped (`stop.sh`), no viewer process runs at all.
- The old Jaeger v2 experiment (same role, replaced by this stack) is archived
  at `~/.ava/traces-viewer/` — stopped, harmless, removable.

## Environment (optional `.env`)

Copy `.env.example` to `.env` (gitignored) to customize. Currently one
variable:

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Public URL of the Grafana UI, used for redirects |

## Verify

```bash
curl -s http://127.0.0.1:3003/api/health          # Grafana: {"database":"ok"}
curl -s 'http://127.0.0.1:3003/api/datasources'   # 3 datasources provisioned
curl -s http://127.0.0.1:9090/api/v1/targets      # 4 scrape targets up
curl -s http://127.0.0.1:3200/ready               # Tempo ready
curl -s http://127.0.0.1:3100/ready               # Loki ready
curl -s http://127.0.0.1:3100/loki/api/v1/label/service/values
  # session logs are flowing when ava-gateway / ava-agent-* appear
```
