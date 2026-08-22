# LGTM observability backend

A **Tempo + Loki + Prometheus + Grafana** stack, co-located with the gateway
host, serving as the cluster's observability backend. Decided 2026-08-11
(task #1170): no Langfuse machine; the most general OTel stack, self-hosted.

The gateway depends on it: this stack is **required serving infrastructure
while the gateway serves /ops and the inspect endpoints**. Live consumers:

- **gateway read paths** — `gateway/loki_events.py` (event history from Loki)
  and `gateway/prom_metrics.py` (telemetry aggregates from Prometheus) back
  the /ops and inspect endpoints.
- **ops alerting** — the Grafana container's embedded Alertmanager evaluates
  the provisioned alert rules and fires the gateway webhook
  (`gateway/routers/alerts.py`); with the stack down, no ops alert fires.
- **events-maintenance** — the rollup aggregates from Loki.
- **`ava cluster health`** — audits crash loops from the Loki event stream
  (`cli/commands/_cluster_health.py`; it fails OPEN — reports healthy — when
  Loki is unreachable, so a down stack silently blinds that audit).

## Lifecycle ownership

The stack is a **host singleton** (fixed host ports, one compose project per
box), owned by the Ava lifecycle on exactly one home: the host carrying the
**`$AVA_HOME/lgtm-host` marker file** (machine-identity-file pattern, created
once by the operator — in practice the prod default home `~/.ava`):

```bash
ava lgtm on   # designate this host/home as the LGTM owner + bring the stack up
```

(`ava lgtm on` writes the marker and runs `start.sh`; `ava lgtm off` removes
it and takes the stack down — volumes persist, so on/off is a clean A/B for
measuring observability's own overhead. `ava lgtm status` shows marker +
containers + readiness probes.)

With the marker present:

- **converge** (every `ava start` / `ava cluster update`, and standalone
  `ava converge`) runs the idempotent `start.sh`;
- the **gateway watchdog** probes the four readiness endpoints every 60s
  (`services/healthchecks/lgtm.py`) and re-runs `start.sh` on a
  connection-level failure — this is the restore mechanism after a
  reboot/OrbStack crash (the compose `restart: unless-stopped` policy only
  fires once the docker daemon itself is back up);
- **`ava status`** shows an LGTM section (compose containers + probes).

A home without the marker never touches the containers — a dev worktree
cluster's converge/watchdog no-op here, so they cannot recreate prod's
running containers from a dev checkout's configs.

## Why this stack

| Criterion | Choice |
|---|---|
| 通用性 | Tempo 是 Grafana Labs 的 OTLP 原生 trace 后端，与 Loki/Prometheus 共用 Grafana 一个 UI |
| 成熟度 | LGTM 是当前自托管可观测性事实标准组合 |
| 本地部署 | docker compose 单机跑 5 容器；OrbStack 已装 |
| UI | Grafana 13（统一 trace/log/metric 探索，Tempo 数据源原生支持 trace 瀑布图） |

Tempo 官方不发布 macOS 二进制（仅 linux/windows），所以当前启动器走
docker compose。容器不是配置边界：Grafana 的固定运行时行为在
`config/grafana/runtime.env`，由当前 compose 启动器和后续 native 启动器
共同消费；CI 也用官方原生 Grafana 二进制验证该文件。Loki/Prometheus/
Grafana 均有原生二进制，启动器原生化时不得把这些行为重新写成一份。
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
watchdog) on host 4318 (OTLP/HTTP). On the gateway it fans out to these
loopback backends. A pure runner instead relays all three signals to the
gateway collector's bearer-authenticated receiver bound only to the exact
`AVA_MACHINE_HOST:4318`; backend ports are never exposed. Every collector
mirrors locally produced traces to `$AVA_HOME/traces/spans.jsonl`. Trace/log
exporters use persistent file-backed queues with unlimited retry; metrics use
a bounded in-memory queue and retry window, then shed old points (cumulative
metrics repair on the next successful sample).

Data lives in docker named volumes (`tempo-data`, `loki-data`, `prom-data`,
`grafana-data`) — `docker compose down` keeps them, `down -v` wipes them.
`restart: unless-stopped`: a crashed/OOM-killed backend recovers by itself and
containers come back with the docker daemon after a reboot; a clean `stop.sh`
(compose down) removes them, so it sticks against the restart policy — but
see the watchdog note below.

Resource + retention posture (single 16GB box shared with the prod cluster):
every container carries explicit `cpus`/`mem_limit` caps (~6.5 cores / ~6GB
ceiling in total), Loki's query fan-out is bounded (24h splits, parallelism 4,
embedded result caches), Prometheus retention is explicit (90d time / 8GB
size — whichever hits first), and Tempo states its 168h block retention
instead of inheriting the upstream default. The unauthenticated backend ports
(Loki 3100, Prometheus 9090, Tempo 3200/14318) are unconditionally bound to
127.0.0.1. Remote writers cross only the authenticated collector receiver on
the gateway's exact private address; no `0.0.0.0`/`::` listener and no backend
bind override exist. Grafana (3003) keeps the wider bind: it is the one
anonymous-but-read-only surface. Grafana has a 2-core / 2GB ceiling; its SQLite
metadata store runs in WAL mode so dashboard readers do not block provisioning
writes. Grafana Live is disabled because no shipped dashboard uses it.

## Start / stop

```bash
bash deploy/lgtm/start.sh   # idempotent; auto-starts OrbStack if needed (macOS)
bash deploy/lgtm/stop.sh    # stops; data persists
```

On the marked LGTM host the lifecycle owns the stack: converge re-runs
`start.sh` on every `ava start`, and the gateway watchdog revives a stack
whose probes hit connection failures within ~a minute. A deliberate stop
there is `ava lgtm off` (removes the marker, then runs `stop.sh`);
`ava start --disable-service lgtm` remains the durable skip that keeps the
designation. While the stack is down the gateway's /ops + inspect reads,
ops alerting, and the events-maintenance rollup degrade; the native sidecar
persists accepted trace/log batches for later delivery. Metrics retry in
bounded memory for 15 minutes and may shed old points; cumulative series
repair on a later successful sample.

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
these files lives at `~/.ava/source/deploy/lgtm/` — it picks the change
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

- Grafana: http://localhost:3003 (anonymous viewer, no login; Viewer may use
  Explore and temporary panel edits but still cannot save dashboards; set
  `GRAFANA_ROOT_URL` in `.env` when the UI is reached through a different
  host, e.g. a VPN overlay address)
  - Tempo datasource (default) — Explore > Traces: search/waterfall once the
    exporter ships traces
  - Loki datasource — Explore > Logs
  - Prometheus datasource — Explore > Metrics
- OTLP ingest: every producer uses its NATIVE local sidecar at
  `http://127.0.0.1:4318`. The gateway additionally serves an authenticated
  receiver at its exact `AVA_MACHINE_HOST:4318` for pure-runner collectors.
  `ava trace ship` bypasses its local sidecar to avoid re-mirroring: gateway
  units send to loopback Tempo; pure runners send to that authenticated remote
  receiver with the cluster bearer.

## Write-side contract (and what "required" means)

- **Producers are untouched**: nothing here writes to `~/.ava/traces` (the
  mirror — the sidecar does), the events table, or any main-flow service. The
  only touch on `$AVA_HOME` is promtail's read-only bind of `$AVA_HOME/logs`
  (session stdout → Loki).
- **The READ side is load-bearing**: the gateway's /ops + inspect endpoints,
  ops alerting (Grafana's Alertmanager → gateway webhook), the
  events-maintenance rollup, and `ava cluster health` all consume this stack
  (see the consumer list at the top). It is not a stop-anytime viewer — on
  the marked host the lifecycle keeps it up.
- The old Jaeger v2 experiment (an earlier viewer-only role, replaced by this
  stack) is archived at `~/.ava/traces-viewer/` — stopped, harmless, removable.

## Environment (optional `.env`)

Copy `.env.example` to `.env` (gitignored) to customize:

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Public URL of the Grafana UI, used for redirects |

Backend bind addresses are deliberately not configurable: Tempo, Loki and
Prometheus stay on loopback in every topology.

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
