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
readiness failure. The launcher resolves the single home-scoped launchd plist
for each backend and skips only when that job is loaded and its endpoint is
reachable. `ava lgtm status` and `ava status` show native job PIDs, the compose
services, and the readiness probes.

The marker gate also protects dev worktrees: their converge and watchdog paths
are no-ops unless that worktree home is explicitly marked.

## What it runs

| Backend | Delivery | Version / limit | Port | Role |
|---|---|---|---|---|
| Loki | native launchd | 3.7.6 / `GOMEMLIMIT=2GiB` | 3100 | log backend, filesystem storage, 7-day retention |
| Prometheus | native launchd | 3.13.2 / `GOMEMLIMIT=1GiB` | 9090 | metrics and OTLP receiver |
| Tempo | remote per cluster config | WSL backend; compose copy is the rollback asset | configured by `AVA_TELEMETRY_TEMPO_ENDPOINT` | trace backend |
| Grafana | native launchd | 13.1.3 | 3003 | anonymous read-only UI |

The pinned release assets and SHA256 values live in
[`native/versions.yml`](native/versions.yml). Converge verifies an archive
before extraction, writes each executable or release tree under
`$AVA_HOME/lgtm/native/`, and records a per-backend version marker. Unsupported
platforms warn and skip: only the designated macOS arm64 host has native assets
today.

Native templates in `native/config/` are rendered on every converge into
`$AVA_HOME/lgtm/native/config/`, including Grafana's INI, runtime environment,
and launch script. Loki's limits and query defenses are copied from the rollback
configuration and must remain aligned. Native backend listeners bind loopback
only, and native Grafana dials Loki, Prometheus, and Postgres on the host
loopback.

## Why this stack

One Grafana UI over Loki, Prometheus, and Tempo gives operators native LogQL,
metrics, and trace exploration while keeping the rest of the product's OTel
pipeline unchanged. Loki, Prometheus, and Grafana use verified native release
assets on the LGTM host, and Tempo is selected by per-cluster configuration.
Grafana and Tempo compose copies are used only during a manual rollback. The
native collector sidecar is still the one
local OTLP entry on port 4318 for this marked home; its filelog receivers also
own session-log shipping. OTLP records carry the home-derived `cluster`
dimension, and the collector rejects non-null values belonging to another home
before they reach Loki, Prometheus, or Tempo.

## Resource and retention posture

Loki and Prometheus use explicit Go memory limits rather than container memory
caps. Loki retains normal streams for seven days, preserves the configured
archive-stream exception, and keeps bounded query splitting, fan-out, and
embedded result caches. Prometheus retains data for 15 days or 8GB, whichever
limit is reached first. Tempo declares its 168-hour block retention. The two
container services retain explicit CPU and memory capsules, and native logs
are written to `$AVA_HOME/lgtm/native/logs/`.

All unauthenticated backend APIs remain loopback-only: Loki 3100, Prometheus
9090, Tempo 3200 and 14318. Grafana 3003 is the intended wider,
anonymous-but-read-only surface.

## Start, stop, and rollback

```bash
ava lgtm on                 # install current native pins, then start native backends
bash deploy/lgtm/start.sh   # idempotent native-only lifecycle launcher
bash deploy/lgtm/stop.sh    # stop native backends
ava lgtm off                # remove marker first, then stop deliberately
```

`start.sh` and `stop.sh` are native-only. The launcher rejects a missing native
binary, Grafana launch script, or missing/ambiguous home-scoped launchd plist;
it bootstraps Loki, Prometheus, or Grafana unless both its launchd job is loaded
and its HTTP listener answers. Before any Loki start or restart it runs
`loki -config.file=$AVA_HOME/lgtm/native/config/loki.yaml -verify-config` and
fails loudly if the rendered config is rejected — a bad `loki.yaml` field
would otherwise crash-loop the launchd job (2026-08-25 incident). It checks
that Grafana provisioned at least 18 alert rules when its admin password file
is available. A newly bootstrapped job
must answer within 30 seconds or the launcher fails loudly. Neither script
touches the Docker daemon or compose.

For a controlled Grafana configuration restart, converge the changed templates
and run `launchctl kickstart -k gui/$(id -u)/com.ava.grafana.<home-slug>`.
The watchdog does not restart a working backend just to apply a configuration
change.

Tempo is remote and selected by `AVA_TELEMETRY_TEMPO_ENDPOINT`; native Grafana
and Prometheus use `AVA_TELEMETRY_TEMPO_QUERY_URL` for queries and scraping.
The local lifecycle neither probes nor manages Tempo. The collector's filelog
receivers ship session and orchestration logs directly to Loki.

## Session logs in Loki

The collector splits raw output into disjoint receivers. `filelog/sessions`
admits only `$AVA_HOME/logs/ava-agent-*-shell-*.out.log` transcripts;
`filelog/services` admits the broad `*.out.log` service set but excludes every
`ava-agent-*` file and the collector's own output; `filelog/orchestration`
ships updater/rollout tees. Agent main stdout is banner-only on this surface,
and its structured records already arrive through OTLP, so excluding it loses
no diagnostic stream while avoiding content-fingerprint collisions.

Both session and service receivers poll every 10 seconds, archive 50 generations
of EOF metadata, and cap discovery at 200 concurrent files. The slower poll cuts
discovery churn 50x, the archive lets a returning EOF file reuse its reader
metadata, and the cap bounds the discovered set. File names become resource
`service.name`, which Loki exposes as `service_name`; read offsets persist under
`$AVA_HOME/otel-collector/log-offsets`, so a restart does not replay history.
Local cleanup is explicit: a daily deployment job runs `ava logs retention`
with `--family-days` to select agent 15d, named PTY shell 7d, gateway/ops/
watchdog 30d, and other service rotations 3d. With neither age flag, the
legacy global threshold remains `AVA_LOG_RETENTION_DAYS` (14d fallback), and
`--older-than` remains its mutually exclusive global override. The command
admits only agent-main logs, named PTY transcript/host logs, and real Loguru
rotation names (including underscore service names such as `delivery_watchdog`)
at the top level of `$AVA_HOME/logs`; it does not recurse, follow symlinks, or
remove files held open by a process. Structured agent logs carry no
`log.file.name`, so the filelog transform leaves them untouched.

## Environment overrides

Copy `.env.example` to `.env` only when an override is needed.

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Grafana redirect URL |
| `GRAFANA_PROVISIONING_PATH` | checkout provisioning directory | Rendered by converge for native Grafana; do not set it in `.env` |
| `AVA_TELEMETRY_TEMPO_ENDPOINT` | `http://127.0.0.1:14318` | Tempo OTLP intake URL for trace export |
| `AVA_TELEMETRY_TEMPO_QUERY_URL` | `http://127.0.0.1:3200` | Tempo query/metrics URL rendered into native Grafana and Prometheus; when Tempo is remote, this host-scoped setting must name its remote query endpoint (writable through the config API), and converge warns when it conflicts with the intake topology |

`.env` holds live secrets and is gitignored; never commit it. Converge renders
the native Grafana provisioning path and runtime configuration; `.env` supplies
only allowed secret and URL overrides.

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
