# Grafana dashboards — Ava Ops (as code)

This directory is the provisioning source. At converge,
`cli/commands/_lgtm_native.py` copies the whole provisioning tree VERBATIM
into `$AVA_HOME/lgtm/native/config/provisioning/` (content-hash user-edit
protected; datasource/webhook URLs are Grafana-native `$__env{}` references,
so the checkout files are always valid); native Grafana receives that
rendered directory's absolute file-provider path through
`GRAFANA_PROVISIONING_PATH` in the rendered `runtime.env` and hot-reloads
changed dashboards within ~30s — a git checkout plus the converge copy is the
whole deployment step, with no separate render pipeline.

## One dashboard (2026-08-23 merge, task #1399)

`ava-ops-main.json` is the **single shipped dashboard** — the four
dashboards (Ava Ops, Plugin Metrics, Overview, Host & Data Plane) were
merged into one, sectioned like Ava Ops (user ruling: "merge into one big
dashboard"). `uid` is fixed at `ava-ops-main` — the dashboard link and user
bookmarks depend on it; never change it.

Seven sections, one row per section — `core` is the 2026-08-06 user-ruling
row header. All sections are **expanded by default** (`collapsed: false`,
2026-08-23 #382):

1. **`core`** — the user's daily first screen: twelve stat tiles cover the
   entire Statistics popover (LLM calls / Warning / Error / Unresolved Warning /
   Unresolved Error / Live agents / LLM cost /
   Tokens / LLM input tokens / LLM output tokens / Cache hit rate / Avg turn
   duration). It then shows Event health,
   Event rate, Token usage — Input, Token usage — Output + Reasoning, Cache
   hit, Turn success rate, and the three full-width **Events** panels:
   business/anomaly logs, the event-type table, and the parse-clean
   raw stream for debugging. The two unresolved-class tiles read the
   events-maintenance daemon's fixed-six-hour Prometheus gauges.
2. **`LLM`** — throughput tokens/s, the three TPS series, calls/bucket,
   cost USD, LLM errors, and per-agent Top 20.
3. **`Gateway & execution`** — gateway latency p50/p95/p99/max + p95/p99 and
   sample count by route, turn duration, exec outcomes, syntax-fix triggers,
   halt classes, SDK Top 20, frontend interactions ×3, settings changes.
4. **`Fleet`** — windowed agent spawns by source, windowed lifecycle totals,
   delivery-stalled total, and SSE backlog.
5. **`Plugin quality`** — the ava_code / ava_fleet / ava_memory panels
   (was the plugins dashboard + the plugin rows; deduplicated).
6. **`Host & data plane`** — the former `ava-host-dataplane` panels: host
   CPU / memory / load / filesystem / disk / network throughput + Postgres
   connections / transactions / size + Redis memory / clients / throughput.
7. **`Cost analysis`** — two cost projections, interval-bucketed cost, and
   Top-20 cost drill-downs by model and agent. Every panel reads usage-time
   `attributes_cost_usd` snapshots from telemetry `llm_usage` events
   (2026-08-23 #384).

The dashboard timezone is `Asia/Shanghai` (2026-08-23 #384). All panels follow
the dashboard time picker; there are no per-panel `timeFrom` overrides.

The dashboard now has 76 panels: core ids remain below 1000 (the four new
stat tiles are 44–47), plugin ids are >= 1000, host/data-plane panels are
2101–2112, the cost-analysis panels are 38, 39, 41–43, and the event panels
are 2201–2203 (business/anomaly logs, event-type table, raw stream). The
duplicate plugin spawn-rate panel (1006) was removed because the Fleet
summaries cover the same information.

## Files

- `ava-ops-main.json` — the only dashboard, hand-maintained (the
  generator did not survive the archive→public port; MetricSpec changes
  are reflected here by hand — see below).
- Deleted 2026-08-23 (a dashboard file removed from this directory is
  dropped from Grafana on the next provisioning reload — `dashboards.yml`
  has `disableDeletion: false`, verified live on the merge day):
  - `ava-ops-plugins.json` — plugin-only subset, fully duplicated in the
    main dashboard.
  - `ava-overview.json` — its Prometheus panels duplicated the richer Loki
    event panels; only the logs panel survived (into `core`), and the
    Tempo panel left with it.
  - `ava-host-dataplane.json` — content merged into the `Host & data
    plane` section. The Resources block on `/insights` was removed 2026-08-24
    (user ruling, task #1479): Grafana's `Host & data plane` section is now the
    only surface for per-host CPU/memory/load/filesystem/disk/network, while
    Insights Status renders Services and a merged Gateway section.

Datasources (provisioned in `../datasources/datasources.yml`): **Loki**
(fixed uid `loki`) for event panels; **Postgres** (uid `ops`) for the `Live
agents` stat (`agents_meta` is not in Loki); **Prometheus** (uid `prometheus`)
for the two unresolved-class tiles, turn-duration percentile alerting, and the
host & data-plane panels
(per-machine OTel Collector sidecar scrapes, `job="ava-infra"` + `host` (OS
hostname) and `machine_name` (Ava roster name) labels; panels group by
`machine_name`). The unresolved tiles read the daemon's absolute
resolution gauges, not raw event lines.

## Core metrics (registered, not hand-written)

Core metric definitions live in `shared/core_metrics_panels.py` (the core
dashboard panels, including the Statistics-coverage tiles) and
`shared/core_metrics_observability.py` (the former `ava_observability`
plugin pack, promoted to core the same day — the repo's own observability
is not a plugin, per user ruling). Both register through
`register_core_metric()` in `shared/core_metrics.py`, which runs the **same
SQL-template safety validation as plugin metrics** (`validate_spec_sql`) and
fills `plugin = "core"`.

All titles are **English** (2026-08-05 user ruling: the previous Chinese
titles could not be changed from the Grafana settings page because the
dashboard is provisioning-managed — titles are edited here, as code).

### MetricSpec — the registration contract

`shared/plugin_metrics.py` defines `MetricSpec`, shared by core and plugin
registrations: `name` / `title` / `description` / `event_name` / `category` /
`unit` / `panel` (`timeseries` / `stat` / `barchart` / `table`) / `query`
(Grafana query template — LogQL over the Loki event stream,
`query_type="logql"`, for event-stream metrics; `query_type="promql"` for the
two fixed-window unresolved gauges; the one SQL holdout is the core `Live
agents` stat over `agents_meta`), plus the Task #882 fields:

- `targets` — extra query templates rendered as refId B/C/... targets on the
  same panel (multi-series panels — e.g. the core TPS panels' max/min-agent
  series); validated like `query`.
- `options` / `custom` / `field_defaults` — optional panel-look overrides
  merged into the generated panel's `options` / `fieldConfig.defaults.custom`
  / `fieldConfig.defaults`.
- `width` / `height` — explicit grid size (override the 6x4 stat / 12x7
  chart default).
- `thresholds` — absolute-threshold steps; an explicit empty list (`[]`)
  suppresses the default green base entirely.

### Loki legend naming

Every Loki target must set `legendFormat`. Aggregates otherwise render their
label-set value (often `{}`), and Grafana `fieldConfig` `byName` display-name
overrides cannot match a Loki target's refId. Use a concise static semantic
name for aggregate series (`"p50"`, `"warn+error"`) and a label template for
grouped series (`"{{attributes_route}}"`, `"{{agent_id}}"`). Do not add a
`byName` display-name override for a Loki target.

### Time granularity

Range panels use fixed windows selected by metric semantics: count trends use
`[5m]`, rates use `[1m]`, calls-per-bucket uses `[30m]`, and Fleet/SSE/delivery
window summaries use instant `[$__range]` queries. Stats and tables remain
instant over `[$__range]`, except the two unresolved-class stats: the
events-maintenance daemon computes their fixed six-hour window and refreshes
their Prometheus gauges every five minutes. Every panel follows the dashboard
time picker; no panel sets a `timeFrom` or fixed `interval` override.

Panels do not set `maxDataPoints`; Grafana derives the `$__interval` step from
the viewport and selected range. That implicit step is the query-weight knob:
when a panel needs a long fixed window, set its interval deliberately instead
of mass-editing targets.

## Writing a plugin metric

1. Add `metrics.py` to your plugin dir (e.g. `ava_builtins/plugins/<name>/metrics.py`).
2. Call `register_metric(MetricSpec(...))` at module top level — the plugin
   name is auto-filled from the import context; do not pass it.
3. The `query` template is **LogQL** (`query_type="logql"`) — the live event
   stream in Loki. Every template must select `{service_name="unknown_service"}`
   and pipeline `| json` before any event-field filter; the template
   contract is validated by `shared/metrics_logql.py`. Use the fixed window
   that matches the panel's information density; stats and tables remain
   instant over `[$__range]`; every count wraps in `sum(...)`.
4. `output` selects the surfaces: `["grafana"]`, `["inspector"]`, or both.
5. **Then update `ava-ops-main.json` by hand**: add the rendered panel
   under the `Plugin quality` row (keep ids >= 1000).
   `tests/plugins/test_plugin_metrics_logql.py` also locks every registered
   grafana spec against the JSON.

Shipped examples: `ava_builtins/plugins/ava_code/metrics.py` (syntax_fix
trend/stat), `ava_builtins/plugins/ava_fleet/metrics.py` (task completion
rate), `ava_builtins/plugins/ava_memory/metrics.py` (recall-filter runs /
empty ratio / error ratio).

## Layout

Greedy 24-column grid, **no overlapping gridPos**: stats 8x4 (three per
row), charts/tables 12x7 (two per row), the business/anomaly event logs 24x7, the raw event stream 24x10, the
event-type table 24x7, row headers h=1 w=24. Rows start exactly at the
previous block's bottom (no gap row).

**Do not add `autofitpanels`**: on Grafana 13.1.x it collapses every panel
to a 30px title bar at narrow window widths.

`refresh` is `10m` and the default window `now-6h` (2026-08-23, task
#1399: the 24h window was the main Loki query-weight driver — 88 Loki
queries × 24h × 5m; 6h/10m keeps the dashboard live while bounding Loki).

## Syncing to the live Grafana

There is no sync: native Grafana (host port 3003) reads this directory through
the absolute `GRAFANA_PROVISIONING_PATH` set in `runtime.env`, and its file
provider reloads a changed file within ~30s. Editing here and checking out on
the LGTM host is the deployment. The `uid` must stay `ava-ops-main`, and
datasource uids must match `datasources.yml`. Loki and Prometheus datasource
URLs use host loopback; Tempo is the remote WSL trace backend.

## Import / update

Provisioning is wired by `dashboards.yml` in this directory (file
provider, `disableDeletion: false`, path expanded from
`$__env{GRAFANA_PROVISIONING_PATH}`). Update flow: edit the dashboard JSON
here (keeping the uid `ava-ops-main`) → land the change → the LGTM host's
checkout advances and the provisioner reloads within ~30s. Restart native
Grafana to force a new provisioning cycle when needed.

## Access requirements

- **Gateway reverse proxy** `/grafana/*` → the Grafana instance, behind the
  normal cluster auth, so the dashboard request is authorized like every other
  API route. The frontend only ever dials `{API_BASE}/grafana/...`.
- **Postgres datasource** reachable from Grafana with read access to
  `agents_meta` (and the `postgres` plugin enabled).
- The Insights page links to `/grafana/d/ava-ops-main` from `/insights#ops`.

## Alerting

**Live**: Grafana Alerting rules, as code in
[`../alerting/rules.yml`](../alerting/rules.yml) — nineteen rules (2026-08-23)
split between the one-minute `ava-ops` group and five-minute `ava-ops-slow`
group over Loki and Prometheus: the event-health,
backlog, restart-spike, LLM-latency, delivery, freshness, trace-watermark,
billing, host/data-plane, and collector-delivery rules, plus the three
slow-request rules R17/R18 (gateway fast-route p95 two-tier + turn-duration
p95, `notify_im: "false"` labels — see the table, threshold calibration,
and notification-channel notes in [`../alerting/README.md`](../alerting/README.md)).
Alerts land in the gateway's `/api/alerts` (Task #1224).
