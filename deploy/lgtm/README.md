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
| Loki | native launchd / user systemd | 3.7.6 / `GOMEMLIMIT=2GiB` | 3100 | log backend, filesystem storage, 7-day retention |
| Prometheus | native launchd / user systemd | 3.13.2 / `GOMEMLIMIT=1GiB` | 9090 | metrics and OTLP receiver |
| Tempo | remote per cluster config | WSL backend; compose copy is the rollback asset | configured by `AVA_TELEMETRY_TEMPO_ENDPOINT` | trace backend |
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
`$AVA_HOME/lgtm/native/logs`. User lingering / WSL boot orchestration is a host
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
caps. Loki retains normal streams for seven days, preserves the configured
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

## Start, stop, and rollback

```bash
ava lgtm on                 # install current native pins, then start native backends
bash deploy/lgtm/start.sh   # idempotent native-only lifecycle launcher
bash deploy/lgtm/stop.sh    # stop native backends
ava lgtm off                # remove marker first, then stop deliberately
```

`start.sh` and `stop.sh` are native-only. The launcher rejects a missing native
binary, Grafana launch script, or missing/ambiguous home-scoped launchd plist;
on Darwin it bootstraps Loki, Prometheus, or Grafana unless its launchd job is loaded
and its HTTP listener answers. Before any Loki start or restart it runs
`loki -config.file=$AVA_HOME/lgtm/native/config/loki.yaml -verify-config` and
fails loudly if the rendered config is rejected — a bad `loki.yaml` field
would otherwise crash-loop the launchd job (2026-08-25 incident). The Darwin script checks
that Grafana provisioned at least 18 alert rules when its admin password file
is available. A newly bootstrapped job
must answer within 30 seconds or the launcher fails loudly. Neither script
touches the Docker daemon or compose.

A converge that changes the rendered Grafana config (INI, runtime env, or the
provisioning tree) kickstarts the running Grafana automatically so the change
takes effect — a running instance never re-reads its INI. For a manual
restart, run `launchctl kickstart -k gui/$(id -u)/com.ava.grafana.<home-slug>`.
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
Local cleanup is explicit and converge-owned: a daily 04:40 job runs `ava logs
rotate` and then `ava logs retention --family-days ...`. Rotation copytruncates
service `.out.log` files and native backend logs at each UTC-day boundary or
when they reach the 64 MiB trigger, so writers keep their open file descriptor.
Retention prunes the resulting archives using agent 15d, named PTY shell 7d,
gateway/ops/watchdog 30d, and other/native archives 3d. With neither age flag,
the legacy global threshold remains `AVA_LOG_RETENTION_DAYS` (14d fallback),
and `--older-than` remains its mutually exclusive global override. Both commands
stay top-level-only, do not follow symlinks, and retention excludes files held
open by a process. Structured agent logs carry no `log.file.name`, so the
filelog transform leaves them untouched.

## Environment overrides

Copy `.env.example` to `.env` only when an override is needed.

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_ROOT_URL` | `http://localhost:3003` | Grafana redirect URL |
| `GRAFANA_PROVISIONING_PATH` | checkout provisioning directory | Rendered by converge for native Grafana; do not set it in `.env` |
| `AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL` | `http://127.0.0.1:3100` / `http://127.0.0.1:9090` | Datasource URLs, expanded by Grafana from `$__env{}` references in the provisioning files. Native converge renders the two-state values (observatory base when `AVA_OBSERVABILITY_URL` is set); compose keeps the static loopback defaults |
| `AVA_PG_URL` | `127.0.0.1:5433` | Read-only Postgres datasource URL (scheme-less host:port); same two-state expansion as the Loki/Prometheus URLs |
| `AVA_ALERTS_WEBHOOK_URL` | `http://127.0.0.1:8000/api/alerts` | Alert webhook URL; two-state: loopback default, or the gateway's reachable address when `AVA_OBSERVABILITY_URL` is set |
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
| Loki/Prometheus/Grafana probes `127.0.0.1:3100/9090/3003` | `deploy/lgtm/start.sh` | Parameterized — probe URLs follow `AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL` / `AVA_TELEMETRY_GRAFANA_URL` (same source as the lgtm healthcheck's readiness probes) |
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
