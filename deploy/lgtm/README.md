# LGTM observability backend

The LGTM host is the cluster's observability backend for logs, metrics, traces,
and the Grafana UI. It is required while the gateway serves `/ops` and the
inspect endpoints: gateway read paths query Loki and Prometheus, Grafana
evaluates alert rules, the events-maintenance rollup reads Loki, and `ava
cluster health` audits Loki event history.

## Lifecycle ownership

The observability station owns its native services by home path. Default
ports preserve the single-station layout; isolated homes require explicit,
non-overlapping native listen ports. Provider identity has two equivalent forms:

- **`$AVA_HOME/lgtm-host` marker** (legacy): `ava lgtm on` writes the marker,
  installs missing pinned native binaries, and runs the idempotent launcher.
  A home without the marker never installs, starts, or stops these backends.
- **`observability-station` unit capability** (declarative): a machine that
  declares the capability (`ava start --serve-observability-station`, or
  `AVA_MACHINE_SERVE_OBSERVABILITY_STATION` / the
  `$AVA_HOME/machine_serve_observability_station` file) converges the full
  native set — configs, native service definitions, storage dirs — with no marker, and its
  watchdog keepalive, producer OTLP export, collector lifecycle, and Loki read
  gates all treat it as the station. The capability is orthogonal to
  `AVA_OBSERVABILITY_URL`: the role decides who PROVIDES the stack, the switch
  decides where CONSUMERS point.

With either form present, every converge runs the same launcher and the gateway
watchdog re-runs it after a connection-level readiness failure. The launcher
uses launchd on Darwin arm64 and user systemd on Linux amd64. It skips a
service only when the owned job and its local HTTP listener are alive. On
Linux, both the loaded unit path and `/proc/<MainPID>/exe` must match this
home. `ava lgtm status` and `ava status` show native PIDs and local probes.

The identity gate also protects dev worktrees: their converge and watchdog
paths are no-ops unless that worktree home is explicitly marked or declares the
capability.

## What it runs

| Backend | Delivery | Version / limit | Port | Role |
|---|---|---|---|---|
| Loki | native launchd / user systemd | 3.7.6 / `GOMEMLIMIT=2GiB` | 3100 | log backend, filesystem storage, 84h retention |
| Prometheus | native launchd / user systemd | 3.13.2 / `GOMEMLIMIT=1GiB` | 9090 | metrics and OTLP receiver |
| Tempo | remote per cluster config | native backend on the station host; compose copy is the rollback asset | configured by `AVA_TELEMETRY_TEMPO_ENDPOINT` | trace backend |
| Grafana | native launchd / user systemd | 13.1.3 | 3003 | anonymous read-only UI |

The pinned release assets and SHA256 values live in
[`native/versions.yml`](native/versions.yml). Converge verifies an archive
before extraction, writes each executable or release tree under
`$AVA_HOME/lgtm/native/`, and records per-backend version and platform markers. A copied Darwin
installation cannot reuse its executable markers on Linux. Pinned assets
support Darwin arm64 and Linux amd64; unsupported platforms warn and skip.

Native templates in `native/config/` are rendered on every converge into
`$AVA_HOME/lgtm/native/config/`, including Grafana's INI, runtime environment,
and launch script. Loki's limits and query defenses are copied from the rollback
configuration and must remain aligned. Native backend listen hosts are config
knobs (`AVA_LGTM_LISTEN_HOST`, `AVA_LGTM_GRAFANA_LISTEN_HOST`): Loki and
Prometheus default to loopback, Grafana defaults to all interfaces, and native
Grafana dials Loki, Prometheus, and Postgres on the host loopback.

**Plugin policy:** the native deployment needs no preinstalled plugins (every
shipped datasource and dashboard panel is a Grafana-core one), so the rendered
INI sets `[plugins] preinstall_disabled = true` — Grafana 13's background
installer would otherwise install the distribution's default plugins on first
start and auto-update them later, work that can still be in flight at SIGTERM
and hold the process past the unit's shutdown deadline (2026-09-10 #2048).
If a plugin ever becomes required, provision it deliberately at converge time;
do not re-enable background installation.

The observation data volume is a per-machine knob: `AVA_LGTM_STORAGE_DIR`
(empty default = `$AVA_HOME/lgtm/native/data`, byte-identical to the historical
layout) moves the Loki filesystem store and the Prometheus TSDB to a configured
path — e.g. a dedicated data volume on a station host. Grafana's own data and
the native logs stay under `$AVA_HOME/lgtm/native` regardless. Switching the
knob does NOT migrate existing data: the new path starts empty, so move the old
store yourself (`rsync` the previous data dir to the new location before the
first start on the new path) or accept the history loss.


### Linux user services and isolated listeners

Linux requires an active user systemd manager (`systemctl --user`). Converge
writes `com.ava.<backend>.<home-slug>.service` into the user's
`$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`). The slug
includes the full home path's hash. Only those exact three units are enabled,
started, restarted, disabled, or removed; other homes and Docker services are
not enumerated or retired. Services recover with `Restart=on-failure`, use a
30-second stop timeout and `KillMode=control-group`, and append logs under
`$AVA_HOME/lgtm/native/logs`. User lingering / boot orchestration is a host
prerequisite, not something `ava lgtm on` silently configures.

Host-scoped ports default to the existing values:

| Setting | Default |
|---|---:|
| `AVA_LGTM_LOKI_PORT` | 3100 |
| `AVA_LGTM_LOKI_GRPC_PORT` | 9095 |
| `AVA_LGTM_PROMETHEUS_PORT` | 9090 |
| `AVA_LGTM_GRAFANA_PORT` | 3003 |

An isolated acceptance home must set all four to unused ports, and point its
consumer query URLs at those listeners. The regular cluster port registry
does not allocate observability ports. These settings do not migrate storage,
change the pinned releases, or alter remote Tempo. On Linux, changed rendered
configs or service definitions restart only already running owned services;
`ava lgtm on` subsequently starts any stopped services. Missing user manager,
invalid Loki config, or failed service operations propagate as failures.

## Why this stack

One Grafana UI over Loki, Prometheus, and Tempo gives operators native LogQL,
metrics, and trace exploration while keeping the rest of the product's OTel
pipeline unchanged. Loki, Prometheus, and Grafana use verified native release
assets on the LGTM host, and Tempo is selected by per-cluster configuration.
Grafana and Tempo compose copies are used only during a manual rollback. The
compose provisioning files are valid as checked in — datasource and webhook
URLs use Grafana's native `$__env{}` expansion against the static defaults in
`config/grafana/runtime.env` (loopback, identical to the native default), so a
compose rollback never needs rendered templates. The
native collector sidecar is still the one
local OTLP entry on port 4318 for this marked home; its filelog receivers also
own session-log shipping. OTLP records carry the home-derived `cluster`
dimension, and the collector rejects non-null values belonging to another home
before they reach Loki, Prometheus, or Tempo.

## Resource and retention posture

Loki and Prometheus use explicit Go memory limits rather than container memory
caps. Loki retains normal streams for 84 hours, preserves the configured
archive-stream exception, and keeps bounded query splitting, fan-out, and
embedded result caches. Prometheus retains data for 15 days or 8GB, whichever
limit is reached first. Tempo declares its 168-hour block retention. The two
container services retain explicit CPU and memory capsules, and native logs
are written to `$AVA_HOME/lgtm/native/logs/`.

All unauthenticated backend APIs remain loopback-only: Loki 3100, Prometheus
9090, Tempo 3200 and 14318. Grafana 3003 is the intended wider,
anonymous-but-read-only surface; its listen host is the
`AVA_LGTM_GRAFANA_LISTEN_HOST` knob, whose `0.0.0.0` default is the historical
all-interfaces form. Widening a listen host past loopback requires the
matching `AVA_TELEMETRY_*_URL` for remote consumers and Prometheus scrape
targets; converge warns on a listen/read mismatch. Local health probes instead
use the native bind settings and bypass HTTP proxy variables. A remote HTTPS
query URL is never interpreted as a local bind address or port.

## OTLP late-sample window (out-of-order intake)

Each machine pushes metrics to Prometheus through an OTel Collector. During a
sleep or link outage, a collector's in-memory queue freezes; on recovery it
replays oldest first with the original event timestamps. Samples arriving
later than Prometheus's `storage.tsdb.out_of_order_time_window` are rejected as
"too old sample". The OTLP write path can then return HTTP 400 for the whole
batch and roll back fresh samples batched alongside the late ones. In task
#4650, this caused 40-60s gaps in other machines' infrastructure series.

From 2026-09-20..23, 43 batches containing 27,154 metric points were dropped,
and `prometheus_tsdb_too_old_samples_total` rose by 2,321. The worst observed
lateness was ~2h15m. The window is raised from 30m to 6h, ~2.7x that observed
delay. A larger window holds out-of-order samples in the TSDB head longer and
costs memory, so a delay beyond 6h should be investigated before increasing
it again. This is an operations setting in both
`deploy/lgtm/native/config/prometheus.yml` and
`deploy/lgtm/config/prometheus.yml`, not a framework constant.

The `ava-ops-prom-too-old-samples` rule fires on any increase in the counter
over 10m. That counter covers both the whole-batch HTTP 400 path and a silent
partial-drop path with no HTTP 400 or collector log line. Once the 6h window
covers the known replay pattern, the rule should remain silent; a firing
instance reports a new, longer-delay episode.

The new window takes effect on the next converge and Prometheus restart. A
live deployment still showing 30m before that rollout is expected. To roll
back, revert the change, render the configs again, and restart Prometheus.

### Acceptance after rollout

First, after converge and restart, read back the rendered native config and
confirm `out_of_order_time_window: 6h`:

```bash
grep 'out_of_order_time_window: 6h' "$AVA_HOME/lgtm/native/config/prometheus.yml"
```

Then confirm the running Prometheus loaded the same value from its status API:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/status/config |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["yaml"])' |
  grep -F 'out_of_order_time_window: 6h'
```

The checked-in container rollback config carries the same setting. After the
next collector recovery event, verify all of the following:

1. The recovering collector's `otlphttp/prometheus` exporter has no new
   `Dropping data` log entry.
2. `prometheus_tsdb_too_old_samples_total` has zero increase across the event
   window. For a one-hour query window on the LGTM host:

   ```bash
   curl -s http://127.0.0.1:9090/api/v1/query \
     --data-urlencode 'query=increase(prometheus_tsdb_too_old_samples_total[1h])'
   ```

3. Other machines in the same batch, such as the company-air/company-mini
   infrastructure series, have no 40-60s collateral gap.

Acceptance is volume-based, not calendar-based: it completes once at least
10 recovery events and at least 27,154 replayed metric points have passed
through the widened window with no too-old rejection — the scale of the
pre-fix evidence (10 drop events, 27,154 points, ~3 events/day,
2026-09-20..23). Track each event's replay volume as the
`prometheus_tsdb_head_out_of_order_samples_appended_total` delta at its
recovery boundary. Reaching both volumes ends the observation; there is no
minimum calendar time, and a short observation is not by itself
insufficient. If an event still drops samples, compare its lateness with
the window before changing anything.

## Start, stop, and rollback

```bash
ava stop -y                  # planned shutdown before changing the root generation
ava lgtm on                  # enable backend intent and invoke normal start
ava lgtm status              # root-owned protocol readiness
ava stop -y
ava lgtm off                 # disable backend intent; preserve data and other choices
```

All three local backends are ordinary root services. Converge prepares assets and
configuration; it never registers or restarts per-backend OS jobs. A repeated
start reuses an unchanged root generation. A changed service set or configuration
requires normal stop before start; it cannot terminate active agent work implicitly.

The native Loki binary validates its rendered config before the root is started.
Readiness combines a live root-owned listener with the backend's successful
protocol response. A response from an unrelated process or an HTTP 503 cannot
certify readiness. The local Loki write/read diagnostic is separate from process
lifecycle and never launches or restarts the stack.

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

All three filelog receivers poll every 30 seconds (orchestration included as of
task #3290 - it previously ran at the unset default of 200ms). The session and
service receivers archive 50 generations of EOF metadata and cap discovery at
200 concurrent files. The slower poll cuts discovery churn 150x, the archive
lets a returning EOF file reuse its reader metadata, and the cap bounds the
discovered set. File names become resource
`service.name`, which Loki exposes as `service_name`; read offsets persist under
`$AVA_HOME/otel-collector/log-offsets`, so a restart does not replay history.
Local cleanup is explicit and converge-owned: a daily 04:40 job runs `ava logs
rotate` and then `ava logs retention --family-days ...`. Rotation copytruncates
service `.out.log` files and native backend logs at each UTC-day boundary or
when they reach the 64 MiB trigger, so writers keep their open file descriptor;
zero-byte files are skipped, so a stale log is not re-archived every day.
Retention prunes the resulting archives using agent 15d, named PTY shell and
computer-use snapshots 7d, gateway/ops/watchdog 30d, and other/native archives
3d. With neither age flag, the legacy global threshold remains
`AVA_LOG_RETENTION_DAYS` (14d fallback), and `--older-than` remains its mutually
exclusive global override. Rotation stays top-level-only; retention reads the
top-level roots plus the fixed nested snapshot dir. Neither follows symlinks,
and retention excludes files held open by a process. Structured agent logs
carry no `log.file.name`, so the filelog transform leaves them untouched.

## Environment overrides

Copy `.env.example` to `.env` only when an override is needed.

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Grafana redirect URL |
| `GRAFANA_PROVISIONING_PATH` | checkout provisioning directory | Rendered by converge for native Grafana; do not set it in `.env` |
| `AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL` | `http://127.0.0.1:3100` / `http://127.0.0.1:9090` | Datasource URLs, expanded by Grafana from `$__env{}` references in the provisioning files. Native converge renders the two-state values (observatory base when `AVA_OBSERVABILITY_URL` is set); compose keeps the static loopback defaults |
| `AVA_PG_URL` | Derived from the direct DB URL (legacy default `127.0.0.1:5433`) | Native converge renders the cluster data plane's scheme-less host:port, resolving a local registered PgBouncer port to PostgreSQL; it never follows the observatory host. A remote pooler with no local registry mapping retains the configured endpoint with the existing direct-URL warning. Manual compose keeps its static default. |
| `AVA_ALERTS_WEBHOOK_URL` | Loopback plus `AVA_GATEWAY_PORT` and `/api/alerts` (default port 8000) | Native converge keeps local Grafana traffic on loopback. With `AVA_OBSERVABILITY_URL` set, it uses `AVA_GATEWAY_URL`, preserving scheme, port and proxy path; a legacy empty URL uses the gateway's reachable host and configured bind port. Manual compose keeps its static default. |
| `AVA_TELEMETRY_TEMPO_ENDPOINT` | `http://127.0.0.1:14318` | Tempo OTLP intake URL for trace export |
| `AVA_TELEMETRY_TEMPO_QUERY_URL` | `http://127.0.0.1:3200` | Tempo query/metrics URL rendered into native Grafana and Prometheus; when Tempo is remote, this host-scoped setting must name its remote query endpoint (writable through the config API), and converge warns when it conflicts with the intake topology |
| `AVA_LGTM_LISTEN_HOST` | `127.0.0.1` | Listen host for the native Loki (HTTP+gRPC) and Prometheus (web) listeners; `0.0.0.0` or a tailnet IP is the external-migration form — the matching `AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL` must follow (converge warns otherwise) |
| `AVA_LGTM_GRAFANA_LISTEN_HOST` | `0.0.0.0` | Listen host for native Grafana's HTTP listener (the historical all-interfaces form); narrow it to `127.0.0.1` or a tailnet IP to restrict the anonymous read-only UI — a specific non-loopback address requires `AVA_TELEMETRY_GRAFANA_URL` to follow (converge warns otherwise) |
| `AVA_TELEMETRY_OTLP_PORT` | `4318` | The OTLP/HTTP ingress port — single source (WP3, task #1945) for the sidecar receiver endpoint, the gateway's authenticated remote receiver + pure-runner relay endpoint, and the roster/healthcheck port probes. Deviating from 4318 also requires `AVA_TELEMETRY_OTLP_ENDPOINT` (the agents' full export URL) to follow |

`.env` holds live secrets and is gitignored; never commit it. Converge renders
the native Grafana provisioning path and runtime configuration; `.env` supplies
only allowed secret and URL overrides.

### GRAFANA_ROOT_URL — migration semantics (task #1945, WP3)

`GRAFANA_ROOT_URL` is Grafana's `root_url` (`deploy/lgtm/native/config/run.sh`
exports it with the `http://localhost:3003` default; `grafana.ini` consumes it
via `$__env{GRAFANA_ROOT_URL}` with `serve_from_sub_path = true`). It is the
base Grafana uses to build redirects and absolute links (login, dashboard
sharing, alert links) — NOT the listener address (that is
`AVA_LGTM_GRAFANA_LISTEN_HOST` + the fixed `3003` http_port).

The default stays loopback because the gateway proxies Grafana through its
authenticated `/grafana/` route, so the browser never dials `:3003` directly.
When the observatory moves to a remote station (stage C of the observatory
migration, `AVA_OBSERVABILITY_URL` set), the *rendered* Grafana is what
migrates: point `GRAFANA_ROOT_URL` at the station's browser-reachable URL
(e.g. the gateway proxy base or the station's tailnet address, keeping
`serve_from_sub_path` semantics) so redirects survive the move. The
docker-compose rollback path reads the same variable (`.env`),
so a shared value stays consistent across both lifecycles. The alert webhook
and datasource URLs are separate settings (`AVA_ALERTS_WEBHOOK_URL` /
`AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL`) — they follow the
observatory independently, per the two-state rules above.

### Localhost-assumption inventory (task #1945, WP3)

The observatory migration (stage C) requires knowing every loopback assumption
on this surface. Status of each, as of WP3:

| Assumption | Where | Status |
|---|---|---|
| Sidecar OTLP receiver `127.0.0.1:4318` | `deploy/otel-collector/otel-collector.yaml` | Parameterized — `AVA_TELEMETRY_OTLP_PORT` (single source, task #1945) |
| Gateway OTLP ingress + runner relay `:4318` | `cli/commands/_otel_collector.py` | Parameterized — same setting |
| Roster gate + healthcheck probes `:4318` | `ops/spec.py`, `services/healthchecks/otel_collector.py` | Parameterized — same setting |
| Agent export endpoint default `http://127.0.0.1:4318` | `shared/config/observability.py` | Default derived from the same constant; the full URL stays a separate override (`AVA_TELEMETRY_OTLP_ENDPOINT`) |
| Loki/Prometheus/Grafana readiness | `shared/lgtm_local.py` | Local native listen settings; external query URLs do not select process ownership. |
| Grafana `root_url` `http://localhost:3003` | `deploy/lgtm/native/config/run.sh` | Deliberately NOT parameterized into a converge render: it is the browser-facing redirect base, resolved at runtime from `GRAFANA_ROOT_URL` (migration section above). Rendered run.sh is asserted byte-identical in `tests/cli/test_converge_lgtm.py` |
| Tempo container-internal OTLP receiver `0.0.0.0:4318` | `deploy/lgtm/config/tempo.yaml` (docker-compose rollback path) | Cannot be parameterized: it is the container-internal contract the compose file maps host `14318` → container `4318`; the host-visible OTLP entry on the LGTM host is `14318` (`AVA_TELEMETRY_TEMPO_ENDPOINT`), and `4318` on the host belongs to the sidecar |
| Test pins `http://127.0.0.1:3200` / `http://127.0.0.1:14318` / `localhost` / `AVA_TELEMETRY_OTLP_PORT=4318` | `tests/conftest.py` | Reviewed WP3: every pin exists to neutralize the operator's ambient `.env` on a dev box (login-shell leak class) and is asserted against both env and settings so a weakened pin fails loudly. `GRAFANA_ROOT_URL` is deliberately not pinned — it never reaches renders (script-level default), only runtime Grafana |

Everything host-visible on the OTLP/LGTM surface now derives from settings;
the two intentional literals left are the Grafana redirect base (runtime env
by design) and the compose container-internal receiver port (mapping
contract).

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
