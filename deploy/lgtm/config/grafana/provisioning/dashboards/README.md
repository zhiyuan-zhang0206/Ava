# Grafana dashboards — Ava Ops (as code)

This directory IS the live provisioning source: the LGTM Grafana container
mounts `deploy/lgtm/config/grafana/provisioning/` read-only at
`/etc/grafana/provisioning`, and its file provider hot-reloads changed
dashboards within ~30s — a git checkout is the whole deployment step, no
copy pipeline.

## One dashboard (2026-08-23 merge, task #1399)

`ava-ops-main.json` is the **single shipped dashboard** — the four
dashboards (Ava Ops, Plugin Metrics, Overview, Host & Data Plane) were
merged into one, sectioned like Ava Ops (user ruling: "merge into one big
dashboard"). `uid` is fixed at `ava-ops-main` — the embed URL and user
bookmarks depend on it; never change it.

Six sections, one row per section — `core` is the 2026-08-06 user-ruling
row header; sections 2–6 are **collapsed by default** (`collapsed: true`):

1. **`core`** — the user's daily first screen: the eight windowed stat
   tiles (LLM calls / Warning / Error / Unresolved Warning / Unresolved
   Error / Live agents / LLM cost (24h) / Tokens (24h)), then Event
   health, Event rate, Token usage — Output + Reasoning, Turn success
   rate, and the full-width **Ava events (Loki)** logs panel (the live
   event stream `{service_name="unknown_service"}` — its former overview
   query `{scope_name="ava.telemetry"}` targeted a label scheme that no
   longer exists and was fixed during the merge).
2. **`LLM`** — throughput tokens/s, token input, the three TPS series,
   cache hit, calls/bucket, cost USD, LLM errors, per-agent Top 20.
3. **`Gateway & execution`** — gateway latency p50/p95/max + by route,
   turn duration, exec outcomes, syntax-fix triggers, halt classes, SDK
   Top 20, frontend interactions ×3, settings changes.
4. **`Fleet`** — agent spawns (by source), lifecycle counts, delivery
   backlog, SSE backlog.
5. **`Plugin quality`** — the ava_code / ava_fleet / ava_memory panels
   (was the plugins dashboard + the plugin rows; deduplicated).
6. **`Host & data plane`** — the former `ava-host-dataplane` panels: host
   CPU / memory / load / filesystem / disk / network throughput + Postgres
   connections / transactions / size + Redis memory / clients / throughput.

Panel content is preserved verbatim, dedup only — nothing was dropped
except the `Recent traces (Tempo)` panel (the Tempo trace UI phase was cut
by decision; it returns in a later phase). Panel ids keep the old ranges:
core < 1000, plugin panels >= 1000; the merged host & data-plane panels
live at 2101–2112 and the logs panel at 2201.

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
    plane` section. Its uid was load-bearing (the status page's Resources
    "History" link); that link now points at ava-ops-main
    (`ui/web/src/app/insights/status/page.tsx`).

Datasources (provisioned in `../datasources/datasources.yml`): **Loki**
(fixed uid `loki`) for every event panel; **Postgres** (uid `ops`) for the
`Live agents` stat (`agents_meta` is not in Loki); **Prometheus** (uid
`prometheus`) for the host & data-plane panels (per-machine OTel Collector
sidecar scrapes, `job="ava-infra"` + a `host` label = the OS hostname).

## Core metrics (registered, not hand-written)

Core metric definitions live in `shared/core_metrics_panels.py` (the
original 16 ops-dashboard panels, migrated 2026-08-06) and
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
`query_type="logql"`, for every shipped metric; the one SQL holdout is the
core `Live agents` stat over `agents_meta`), plus the Task #882 fields:

- `targets` — extra SQL templates rendered as refId B/C/... targets on the
  same panel (multi-series panels — e.g. the core TPS panels' max/min-agent
  series); validated like `query`.
- `options` / `custom` / `field_defaults` — optional panel-look overrides
  merged into the generated panel's `options` / `fieldConfig.defaults.custom`
  / `fieldConfig.defaults`.
- `width` / `height` — explicit grid size (override the 6x4 stat / 12x7
  chart default).
- `thresholds` — absolute-threshold steps; an explicit empty list (`[]`)
  suppresses the default green base entirely.

## Writing a plugin metric

1. Add `metrics.py` to your plugin dir (e.g. `ava_builtins/plugins/<name>/metrics.py`).
2. Call `register_metric(MetricSpec(...))` at module top level — the plugin
   name is auto-filled from the import context; do not pass it.
3. The `query` template is **LogQL** (`query_type="logql"`) — the live event
   stream in Loki. Every template must select `{service_name="unknown_service"}`
   and pipeline `| json` before any event-field filter; the template
   contract is validated by `shared/metrics_logql.py`. Stat panels run as
   instant queries over `[$__range]`; timeseries/barchart panels as range
   queries bucketed by `[$__interval]`; every count wraps in `sum(...)`.
4. `output` selects the surfaces: `["grafana"]`, `["inspector"]`, or both.
5. **Then update `ava-ops-main.json` by hand**: add the rendered panel
   under the `Plugin quality` row (keep ids >= 1000) and bump
   `EMBED_HEIGHT` in `ui/web/src/app/insights/ops/page.tsx` — its
   `page.test.tsx` derives the expected height from the JSON's gridPos and
   fails loudly when they drift. `tests/plugins/test_plugin_metrics_logql.py`
   also locks every registered grafana spec against the JSON.

Shipped examples: `ava_builtins/plugins/ava_code/metrics.py` (syntax_fix
trend/stat), `ava_builtins/plugins/ava_fleet/metrics.py` (task done rate,
spawn frequency), `ava_builtins/plugins/ava_memory/metrics.py` (recall-filter
runs / empty ratio / error ratio).

## Layout & embed height

Greedy 24-column grid, **no overlapping gridPos**: stats 8x4 (three per
row), charts/tables 12x7 (two per row), the logs panel 24x10, row headers
h=1 w=24. Rows start exactly at the previous block's bottom (no gap row).

The embed is a full-height iframe with **no inner scrollbar**:
`EMBED_HEIGHT = grid rows × 30px + (rows-1) × 8px + 116px chrome` (209
rows → 8050px after the merge, Grafana 13.1.3). `page.test.tsx` derives the
expected value from the JSON and fails on drift — bump
`ui/web/src/app/insights/ops/page.tsx` whenever panels change. Note the
frame keeps the full **expanded** height while sections 2–6 start
collapsed, so the embed shows blank space below `core` until a section is
expanded — a consequence of the collapse-by-default design, not a layout
bug. **Do not add `autofitpanels`**: on Grafana 13.1.x it collapses every
panel to a 30px title bar at embed widths.

`refresh` is `10m` and the default window `now-6h` (2026-08-23, task
#1399: the 24h window was the main Loki query-weight driver — 87 Loki
queries × 24h × 5m; 6h/10m keeps the embed live while bounding Loki).

## Syncing to the live Grafana

There is no sync: the LGTM Grafana container (host port 3003,
`deploy/lgtm` compose) mounts this directory read-only and its file provider
reloads a changed file within ~30s — editing here and checking out on the
LGTM host is the deployment. The `uid` must stay `ava-ops-main`; datasource
uids must match `datasources.yml`. Loki and Prometheus now run natively on the
host, so their Grafana datasource URLs use `host.docker.internal`; Tempo stays
an in-compose container.

## Import / update

Provisioning is wired by `dashboards.yml` in this directory (file
provider, `disableDeletion: false`, path
`/etc/grafana/provisioning/dashboards` inside the container). Update flow:
edit the dashboard JSON here (keeping the uid `ava-ops-main`) → land the
change → the LGTM host's checkout advances and the provisioner reloads
within ~30s (a `docker restart lgtm-grafana-1` forces the reload — Grafana
does not hot-apply provisioned dashboard files without a reload of the
provisioning cycle, and panel edits are only picked up on restart).

## Embed requirements (deployment-side, to verify)

- **Gateway reverse proxy** `/grafana/*` → the Grafana instance, behind the
  normal cluster auth, so the iframe request is authorized like every other
  API route. The frontend only ever dials `{API_BASE}/grafana/...`.
- **`allow_embedding = true`** in Grafana config (default `false` sends
  `X-Frame-Options: deny`, which blocks the iframe).
- **Postgres datasource** reachable from Grafana with read access to
  `agents_meta` (and the `postgres` plugin enabled).
- The embed renders at `/insights#ops` in light and dark theme, `&kiosk`
  hides Grafana chrome (sidemenu/topnav) while the dashboard timepicker
  stays usable, panels render at natural size, and the iframe is
  full-height.

## Alerting

**Live**: Grafana Alerting rules, as code in
[`../alerting/rules.yml`](../alerting/rules.yml) — nineteen rules (2026-08-23)
evaluated every minute over Loki and Prometheus: the event-health,
backlog, restart-spike, LLM-latency, delivery, freshness, trace-watermark,
billing, host/data-plane, and collector-delivery rules, plus the three
slow-request rules R17/R18 (gateway fast-route p95 two-tier + turn-duration
p95, `notify_im: "false"` labels — see the table, threshold calibration,
and notification-channel notes in [`../alerting/README.md`](../alerting/README.md)).
Alerts land in the gateway's `/api/alerts` (Task #1224).
