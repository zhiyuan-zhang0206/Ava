# LGTM observability backend

The LGTM host is the cluster's observability backend for logs, metrics, traces,
and the Grafana UI. It is required while the gateway serves `/ops` and the
inspect endpoints: gateway read paths query Loki and Prometheus, Grafana
evaluates alert rules, the events-maintenance rollup reads Loki, and `ava
cluster health` audits Loki event history.

## Lifecycle ownership

This is a host singleton with fixed ports, owned by the one home carrying
`$AVA_HOME/lgtm-host`. A home without the marker never installs, starts, or
stops these backends.

```bash
ava lgtm on
```

`ava lgtm on` writes the marker, installs missing pinned native binaries, and
runs the idempotent launcher. With the marker present, every converge runs the
same launcher and the gateway watchdog re-runs it after a connection-level
readiness failure. The launcher probes native processes first, so a live Loki
or Prometheus process is never restarted by the watchdog. `ava
lgtm status` and `ava status` show native job PIDs, the compose services, and
the readiness probes.

The marker gate also protects dev worktrees: their converge and watchdog paths
are no-ops unless that worktree home is explicitly marked.

## What it runs

| Backend | Delivery | Version / limit | Port | Role |
|---|---|---|---|---|
| Loki | native launchd | 3.7.6 / `GOMEMLIMIT=2GiB` | 3100 | log backend, filesystem storage, 7-day retention |
| Prometheus | native launchd | 3.13.2 / `GOMEMLIMIT=1GiB` | 9090 | metrics and OTLP receiver |
| Tempo | compose container | `grafana/tempo:3.0.2`, 1 core / 768MiB | 3200, 14318 | trace backend |
| Grafana | compose container | `grafana/grafana:13.1.3`, 2 cores / 2GiB | 3003 | anonymous read-only UI |

The pinned release assets and SHA256 values live in
[`native/versions.yml`](native/versions.yml). Converge verifies an archive
before extraction, writes it under `$AVA_HOME/lgtm/native/bin/`, and records a
per-backend version marker. Unsupported platforms warn and skip: only the
designated macOS arm64 host has native assets today.

Native templates in `native/config/` are rendered on every converge into
`$AVA_HOME/lgtm/native/config/`. Loki's limits and query defenses are copied
from the rollback configuration and must remain aligned. Native backend
listeners bind loopback only, and native Grafana dials Loki, Prometheus, and
Postgres on the host loopback.

## Why this stack

One Grafana UI over Loki, Prometheus, and Tempo gives operators native LogQL,
metrics, and trace exploration while keeping the rest of the product's OTel
pipeline unchanged. Loki and Prometheus use verified native release assets on
the LGTM host. The native collector sidecar is still the one local OTLP entry
on port 4318; its filelog receivers also own session-log shipping.

## Resource and retention posture

Loki and Prometheus use explicit Go memory limits rather than container memory
caps. Loki retains normal streams for seven days, preserves the configured
archive-stream exception, and keeps bounded query splitting, fan-out, and
embedded result caches. Prometheus retains data for 90 days or 8GB, whichever
limit is reached first. Tempo declares its 168-hour block retention. The two
container services retain explicit CPU and memory capsules, and native logs
are written to `$AVA_HOME/lgtm/native/logs/`.

All unauthenticated backend APIs remain loopback-only: Loki 3100, Prometheus
9090, Tempo 3200 and 14318. Grafana 3003 is the intended wider,
anonymous-but-read-only surface.

## Start, stop, and rollback

```bash
ava lgtm on                 # install current native pins, then start all backends
bash deploy/lgtm/start.sh   # idempotent lifecycle launcher
bash deploy/lgtm/stop.sh    # stop native jobs and Tempo/Grafana; keep volumes
ava lgtm off                # remove marker first, then stop deliberately
```

`start.sh` rejects a missing native binary before it touches Docker. It starts
the Docker daemon when necessary, runs `docker compose up -d` for Tempo and
Grafana, and bootstraps a native job only when its HTTP listener does not
answer. A newly bootstrapped job must answer within 30 seconds or the launcher
fails loudly.

For a controlled configuration restart, converge the changed templates and
then use `ava lgtm off` followed by `ava lgtm on`. This is deliberate: the
watchdog does not restart a working backend just to apply a configuration
change.

Rollback keeps the retained compose data volumes. Stop the hybrid stack,
restore the earlier `docker-compose.yml` and backend configs from git, then
run `docker compose up -d`; Promtail's native binary and positions path are no
longer part of the stack because collector filelog receivers replace them.

## Session logs in Loki

The collector's `filelog/sessions` and `filelog/orchestration` receivers ship
session stdout and orchestration tee logs. The file name becomes resource
`service.name`, which Loki exposes as the `service_name` stream label. Read
offsets persist under `$AVA_HOME/otel-collector/log-offsets`, so a collector
restart does not replay the log history. Structured agent logs still arrive
through the collector's OTLP receiver and carry no `log.file.name`, so the
filelog transform leaves them untouched.

## Environment overrides

Copy `.env.example` to `.env` only when an override is needed.

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Grafana redirect URL |
| `GRAFANA_PROVISIONING_PATH` | source checkout's dashboard provisioning directory | Native Grafana file-provider path, set by `config/grafana/runtime.env` |

## Verify

```bash
curl -s http://127.0.0.1:3003/api/health
curl -s http://127.0.0.1:9090/api/v1/targets
curl -s http://127.0.0.1:3200/ready
curl -s http://127.0.0.1:3100/ready
curl -s http://127.0.0.1:3100/loki/api/v1/label/service/values
```

The Grafana datasource list remains available at
`http://127.0.0.1:3003/api/datasources`.
