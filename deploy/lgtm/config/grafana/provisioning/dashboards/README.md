# Grafana dashboards — Ava Ops (as code)

This directory is the provisioning source. At converge,
`cli/commands/_lgtm_native.py` renders the whole provisioning tree into
`$AVA_HOME/lgtm/native/config/provisioning/` (content-hash user-edit
protected; datasource/webhook URLs are Grafana-native `$__env{}` references,
so the checkout files are always valid). Every file is copied VERBATIM
except `ava-ops-main.json`, which is generated from the metric registries
(task #3697 S3) — the checked-in copy here is the render's reference output,
not the provisioning source. Native Grafana receives the rendered
directory's absolute file-provider path through `GRAFANA_PROVISIONING_PATH`
in the rendered `runtime.env` and hot-reloads changed dashboards within
~30s — a git checkout plus converge is the whole deployment step.

## One dashboard (2026-08-23 merge, task #1399)

`ava-ops-main.json` is the **single shipped dashboard** — the four
dashboards (Ava Ops, Plugin Metrics, Overview, Host & Data Plane) were
merged into one, sectioned like Ava Ops (user ruling: "merge into one big
dashboard"). `uid` is fixed at `ava-ops-main` — the dashboard link and user
bookmarks depend on it; never change it.

Ten sections, one row per section — `core` is the 2026-08-06 user-ruling
row header, and every metric-shipping plugin owns a row named after it (the
per-plugin rows the 2026-08-23 merge had collapsed into `Plugin quality`,
restored by task #3689). All sections are **expanded by default**
(`collapsed: false`, 2026-08-23 #382):

1. **`core`** — the user's daily first screen: twelve stat tiles cover the
   entire Statistics popover (LLM calls / Warning / Error / Unresolved Warning /
   Unresolved Error / Live agents / LLM cost /
   Tokens / LLM input tokens / LLM output tokens / Cache hit / Avg turn
   duration). It then shows Event health,
   Event rate, Token usage — Input, Token usage — Output + Reasoning, Cache
   hit, Turn success rate, and the three full-width **Events** panels:
   business/anomaly logs, the event-type table, and the parse-clean
   raw stream for debugging. The four resolution tiles read the
   events-maintenance daemon's fixed-six-hour Prometheus gauges: Warning /
   Error (Loki `$__range` totals) sit beside Dismissed Warning / Dismissed
   Error and Unresolved Warning / Unresolved Error, so the user-visible trio
   (total / resolved / net) sums by construction at the default six-hour
   window (task #1935).
2. **`LLM`** — throughput tokens/s, the three TPS series, calls/bucket,
   cost USD, LLM errors, provider stalls, and per-agent Top 20.
3. **`Gateway & execution`** — gateway latency p50/p95/p99/max + p95/p99 and
   sample count by route, turn duration, exec outcomes, syntax-fix triggers,
   halt classes, SDK Top 20, frontend interactions ×3, settings changes.
4. **`Fleet`** — windowed agent spawns by source, windowed lifecycle totals,
   delivery-stalled total, SSE backlog, and the Max Agent ID growth curve
   (the gateway's 60s `agent_registry` gauge + its deriv rate, task #2010).
5. **`ava_code`** — the syntax-fix metric panels: fix count (per minute)
   and fixes (window).
6. **`ava_fleet`** — the task-completion-rate panel.
7. **`ava_memory`** — recall-filter runs / empty ratio / error ratio /
   failures plus the passive-recall search and filter latencies.
8. **`Host & data plane`** — the former `ava-host-dataplane` panels: host
   CPU / memory / load / filesystem / disk / network throughput + Postgres
   connections / transactions / size + Redis memory / clients / throughput.
9. **`Cost analysis`** — two cost projections, interval-bucketed cost, and
   Top-20 cost drill-downs by model and agent. Every panel reads usage-time
   `attributes_cost_usd` snapshots from telemetry `llm_usage` events
   (2026-08-23 #384).
10. **`PR flow`** — PR ready→merged median/p90 by day, Trunk queue depth,
   QA rounds (mean + re-review share) by day, and new flaky quarantines by
   day, from the daily export job's Prometheus gauges (task #2139).

The dashboard timezone is `Asia/Shanghai` (2026-08-23 #384). All panels follow
the dashboard time picker; there are no per-panel `timeFrom` overrides.

Panel titles state their time basis wherever the rendered number would not
reveal it (user ruling 2026-09-14, task #3362): `(per minute)` on per-minute
rate panels, `(window)` on panels whose value is a selected-window total or
average over `$__range`. Rates whose unit is already self-evident (`/ minute`,
`/s`, TPS, `events/s`) carry no extra qualifier, and smoothing-bucket widths
stay in panel descriptions.

The `PR flow` row (task #2139) reads the daily macmini export job
(`scripts/pr_flow_export.py`, 00:25 cluster time) back as Prometheus gauges:
one absolute sample per complete cluster-tz day, re-emitted on every run so
the trailing window stays visible. Each by-day tile is a **fixed-lookback**
instant query, `max by (day) (last_over_time(<gauge>[26h]))` — 26h keeps a
day's last sample alive across the daily cadence, and a day drops out of its
table once the sample ages past it (a missed run shows as a gap, not a stale
number). The three tables need their joinByField(`day`) transformation for
the same reason: the instant queries return one frame per day series, and
without the join the table falls back to a per-series frame picker instead
of one row per day.

The dashboard now has 95 panel entries (85 panels + 10 row headers): core
ids remain below 1000 (the four new stat tiles are 44–47), plugin ids are
>= 1000 (the three plugin rows are 1001 / 1004 / 1007; their panels are
1002–1013), host/data-plane panels are 2101–2112, the cost-analysis panels are
38, 39, 41–43, the event panels are 2201–2203 (business/anomaly logs,
event-type table, raw stream), the Fleet growth panels are 2301–2302
(Max Agent ID + deriv rate, task #2010), and the PR-flow row is 2008 with
panels 2401–2404 (task #2139). The
duplicate plugin spawn-rate panel (1006) was removed because the Fleet
summaries cover the same information.

## Files

- `ava-ops-main.json` — the only dashboard, rendered from the metric
  registries since task #3697: `shared/grafana_dashboard.py` renders the
  registries into this file's shape (every panel registry-covered, slice
  S2), `ava lgtm render` previews (diff) or force-writes the host
  provisioning copy, and converge generates its provisioning copy from the
  same render (slice S3) — plugin installs/uninstalls and MetricSpec
  changes move the panels with no hand mirror here; the checked-in copy is
  the render's reference output.
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
for the four resolution tiles, the fixed gauges published by the gateway and
the PR-flow export job (Fleet growth task #2010, PR flow task #2139),
turn-duration percentile alerting, and the
host & data-plane panels
(per-machine OTel Collector sidecar scrapes, `job="ava-infra"` + `host` (OS
hostname) and `machine_name` (Ava roster name) labels; panels group by
`machine_name`). The unresolved tiles read the daemon's absolute
resolution gauges, not raw event lines.

## Core metrics (registered, not hand-written)

Core metric definitions live in `shared/core_metrics_panels.py` (the core
dashboard panels, including the Statistics-coverage tiles),
`shared/core_metrics_observability.py` (the former `ava_observability`
plugin pack, promoted to core the same day — the repo's own observability
is not a plugin, per user ruling), `shared/core_metrics_events.py` (the
event-stream panels: the Events trio and the gateway sample count) and
`shared/core_metrics_host.py` (the `Host & data plane` section), plus the
smaller registration modules beside them (`core_metrics_cost` and
`core_metrics_frontend` — the line budget splits of the first two, task
#3697 — and `core_metrics_dismissed`, `core_metrics_fleet`,
`core_metrics_pr_flow`). All register through `register_core_metric()` in
`shared/core_metrics.py`, which runs the **same
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
Prometheus-gauge tiles (resolution, Fleet growth, memory search, PR flow);
the one SQL holdout is the core `Live
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
- `panel_id` / `section` / `order` / `position` / `transformations` —
  dashboard placement pins (task #3697): the as-is panel id and section row
  (core panels), the render rank within the section, an absolute grid
  position for the rare flow deviation, and verbatim Grafana transformations
  (the PR-flow day tables' `joinByField` + `organize`). Plugin panel ids are
  allocated per plugin block, sorted by plugin name from 1001.

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
instant over `[$__range]`, except the four resolution stats: the
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
   and pipeline `| json`; since the 2026-08-23 index-label cutover (task
   #1467) the `event_name`/`agent_id` matchers go INSIDE the stream selector
   (`{service_name="unknown_service", event_name=...}`) and `| json` stays
   for the level/category/attributes filters (those fields are not stream
   labels); the template contract is validated by `shared/metrics_logql.py`.
   Use the fixed window
   that matches the panel's information density; stats and tables remain
   instant over `[$__range]`; every count wraps in `sum(...)`.
4. `output` selects the surfaces: `["grafana"]`, `["inspector"]`, or both.
5. **No hand edit**: converge generates `ava-ops-main.json` from the metric
   registries (task #3697 S3) and `ava lgtm render` previews the result —
   the panel lands under the row named after the plugin (every
   metric-shipping plugin owns a row; ids >= 1000 are renderer-allocated).
   `tests/plugins/test_plugin_metrics_logql.py` also locks every registered
   grafana spec against the JSON.

Shipped examples: `ava_builtins/plugins/ava_code/metrics.py` (syntax_fix
trend/stat), `ava_builtins/plugins/ava_fleet/metrics.py` (task completion
rate), `ava_builtins/plugins/ava_memory/metrics.py` (recall-filter runs /
empty ratio / error ratio plus passive-recall search and filter latency).

## Layout

Greedy 24-column grid, **no overlapping gridPos**: stats 8x4 (three per
row), charts/tables 12x7 (two per row), the business/anomaly event logs 24x7, the raw event stream 24x10, the
event-type table 24x7, row headers h=1 w=24. Rows start exactly at the
previous block's bottom (no gap row) — the renderer's flow engine implements
exactly this, plus the explicit spec pins (task #3697): the one pinned panel
position and the one historical three-row gap before the `LLM` row.

**Do not add `autofitpanels`**: on Grafana 13.1.x it collapses every panel
to a 30px title bar at narrow window widths.

`refresh` is `10m` and the default window `now-24h` (2026-09-14, user
request; the earlier `now-6h` default, 2026-08-23 task #1399, was chosen to
bound Loki query weight — 88 Loki queries × 24h × 5m; with the 24h default,
watch Loki panel latency and shrink the window again if it degrades).

## Syncing to the live Grafana

There is no sync: native Grafana (host port 3003) reads the rendered
directory through the absolute `GRAFANA_PROVISIONING_PATH` set in
`runtime.env`, and its file provider reloads a changed file within ~30s.
Editing a file here and checking out on the LGTM host is the deployment —
except `ava-ops-main.json`, which converge generates from the metric
registries (edit the specs, not the JSON). The `uid` must stay `ava-ops-main`, and
datasource uids must match `datasources.yml`. Loki and Prometheus datasource
URLs use host loopback; Tempo is the station-native trace backend.

## Import / update

Provisioning is wired by `dashboards.yml` in this directory (file
provider, `disableDeletion: false`, path expanded from
`$__env{GRAFANA_PROVISIONING_PATH}`). Update flow: land the change in the
metric registries (or edit a verbatim provisioning file here) → the LGTM
host's checkout advances, converge regenerates the rendered tree, and the
provisioner reloads within ~30s. Restart native Grafana to force a new
provisioning cycle when needed.

## Access requirements

- **Gateway reverse proxy** `/grafana/*` → the Grafana instance, behind the
  normal cluster auth, so the dashboard request is authorized like every other
  API route. The frontend only ever dials `{API_BASE}/grafana/...`.
- **Postgres datasource** reachable from Grafana with read access to
  `agents_meta` (and the `postgres` plugin enabled).
- The Insights page links to `/grafana/d/ava-ops-main` from `/insights#ops`.

## Alerting

**Live**: Grafana Alerting rules, as code in
[`../alerting/rules.yml`](../alerting/rules.yml) — thirty-eight rules (2026-09-18)
split between the one-minute `ava-ops` group and five-minute `ava-ops-slow`
group over Loki and Prometheus: the event-health,
backlog, restart-spike, LLM-latency, delivery, freshness, trace-watermark,
billing, provider stall/pair, host/data-plane, and collector-delivery rules, plus the three
slow-request rules R17/R18 (gateway fast-route p95 two-tier + turn-duration
p95, `notify_im: "false"` labels — see the table, threshold calibration,
and notification-channel notes in [`../alerting/README.md`](../alerting/README.md)).
Alerts land in the gateway's `/api/alerts` (Task #1224).
