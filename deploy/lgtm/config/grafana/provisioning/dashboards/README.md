# Grafana dashboards — Ava Ops (as code)

This directory IS the live provisioning source: the LGTM Grafana container
mounts `deploy/lgtm/config/grafana/provisioning/` read-only at
`/etc/grafana/provisioning`, and its file provider hot-reloads changed
dashboards within ~30s — a git checkout is the whole deployment step, no
copy pipeline.

One dashboard embedded at `/insights#ops` — `ava-ops-main` (Task #882): a
**core section first** (row header `core` — 2026-08-06 user ruling), then
**one row per plugin** (row header = plugin name). A single full-height
iframe carries both tiers. The frontend loads it through the gateway's
`/grafana` reverse proxy as

```
/grafana/d/ava-ops-main?theme=<light|dark>&kiosk
```

(`ui/web/src/app/insights/ops/page.tsx` builds the URL. Since 2026-08-05
the URL carries no `from`/`to`: the frontend window selector was removed —
the time range and refresh interval are Grafana's native timepicker, the
dashboard's own defaults apply. `kiosk` hides the Grafana sidemenu/topnav
but keeps the timepicker. Before 2026-08-06 plugin metrics were registered
and generated but never surfaced; since the two-tier rework both core and
plugin metrics render inside this single dashboard, and the separate
plugins embed is gone (`PLUGINS_EMBED_HEIGHT` was removed — there is only
one iframe.)

## Files

- `ava-ops-main.json` — the **full dashboard** (core section + plugin
  sections), generated. `uid` is fixed at `ava-ops-main` — the embed URL
  depends on it; never change it.
- `ava-ops-plugins.json` — the **plugin-only intermediate**, generated in
  the same run. It is *not* embedded anywhere — kept for tests / standalone
  use (uid `ava-ops-plugins`).
- `ava-host-dataplane.json` — the **traditional SRE layer** (issue #46),
  hand-written against the Prometheus datasource: a `host` row (CPU,
  memory, load, filesystem, disk and network throughput) and a `data plane`
  row (Postgres connections / transactions / database size, Redis memory /
  clients / evictions / throughput). Its series come from each machine's
  OTel Collector sidecar (`host_metrics` + `postgresql` + `redis`
  receivers), not from a node_exporter, and carry `job="ava-infra"` plus a
  `host` label = the OS hostname — deliberately not `machine`, which app
  metrics use for the Ava machine name. uid `ava-host-dataplane`; the status
  page's Resources section links to it, so the uid is load-bearing. Every
  expression in it was evaluated against a live Prometheus 3.13.2 before it
  landed.
- Datasource is **Loki** (fixed uid `loki`, provisioned in
  `../datasources/datasources.yml`) — every panel reads the live event stream
  through LogQL (task #1280 / #180: the PG `events` table is a frozen
  archive since the LGTM cutover). The one exception is the **Live agents**
  stat (`core_live_agents`), which reads the live `agents_meta` table on the
  **PostgreSQL** datasource (fixed uid `ops`) by design.

Both JSON files were **generated** from the metric registrations (below).
The generator did not survive the archive→public port (see the plugin
section) — until it returns, edits must keep the JSONs consistent with the
registered specs.

## Core metrics (registered, not hand-written)

Core metric definitions live in `shared/core_metrics_panels.py` (the
original 16 ops-dashboard panels, migrated 2026-08-06) and
`shared/core_metrics_observability.py` (the former `ava_observability`
plugin pack, promoted to core the same day — the repo's own observability
is not a plugin, per user ruling). Both register through
`register_core_metric()` in `shared/core_metrics.py`, which runs the **same
SQL-template safety validation as plugin metrics** (`validate_spec_sql`) and
fills `plugin = "core"` — the row header and display name are "core".

The generator renders the core section first: one row panel titled `core`
(id = 900), then the core panels in registration order (panels module
first, then observability). Core panel ids stay < 1000; plugin panels
reserve the >= 1000 range.

### The 16 ops panels (core_metrics_panels.py)

All titles are **English** (2026-08-05 user ruling: the previous Chinese
titles could not be changed from the Grafana settings page because the
dashboard is provisioning-managed with `allowUiUpdates: false` — titles are
edited here, as code). The Chinese metrics-page labels from #787 were a
separate surface (`agents.label` data, retired with the Metrics page).

Summary stats (windowed), two rows of three (2026-08-05 user ruling):
Row 1: **LLM calls** — `llm_usage` count in window; **Warning** — `level =
'warning'` count; **Error** — `level IN ('error','critical')` count (the
old combined WARNING+ERROR stat split into two blocks; the
`delivery_stalled` oldest-backlog stat was dropped to make room — the
backlog stays visible in the SSE-backlog chart).
Row 2: **Live agents** — `agents_meta` rows with status running/idling (the
`agent_restarted` stat was removed 2026-08-05 — restart metrics retired);
**LLM cost (24h)** — USD spend from the same pricing-CASE the observability
core pack generates (kept in sync via `shared/lm/pricing.py`); **Tokens
(24h)** — Σ in_total + Σ out_total (incl. reasoning + cache_read).

Time series / bars (one line each — the exact SQL and per-panel look live in
`core_metrics_panels.py`, the panel *specs* are the source of truth):

1. **SSE backlog — delivery_stalled (by stall seconds)** — stacked bars by
   stall-seconds band; the query zero-fills every bucket via
   `generate_series` (Grafana's `$__timeGroup(..., fill)` does not work on
   this version), so a quiet pipeline renders an empty grid, not "No data".
2. **LLM throughput tokens/s** — Σ tokens ÷ Σ latency seconds per bucket.
3. **LLM calls / bucket** — call rate per bucket.
4. **Event health — WARNING+ERROR vs total** — event count per bucket.
5. **Token usage — Output + Reasoning** — per-bucket Σ `out_total` (already
   includes reasoning tokens) and Σ `reasoning`, own axis — split from the
   old combined in/out/reasoning panel 2026-08-05 because input volume is
   an order of magnitude larger.
6. **Token usage — Input** — per-bucket Σ `in_total` (incl. cache_read),
   its own axis. Input and output/reasoning live on separate charts so
   neither scale flattens the other.
7. **Cache hit (token-weighted / max agent / min agent)** — per-bucket
   weighted-average cache hit (Σ cache_read ÷ Σ in_total, token-weighted
   across all calls) plus the per-bucket best and worst agent.
8. **Input TPS (avg / max agent / min agent)** — per-bucket input-token
   throughput: Σ in_total ÷ Σ latency_sec (tok/s), plus best/worst agent.
   `in_total` includes cache_read (≈99.5% cache-hit recently), so this is
   prefill/cache-read throughput.
9. **Output TPS (avg / max agent / min agent)** — per-bucket output-token
   throughput: Σ out_total ÷ Σ latency_sec (tok/s), plus best/worst agent.
   `out_total` already includes reasoning, so this is pure decode speed.
10. **Gen-stage output TPS (avg / max agent / min agent)** — per-bucket
    generation-stage throughput: Σ out_total ÷ Σ decode_sec (tok/s), plus
    best/worst agent. `decode_ms` is the W14 (2026-08-04) instrumentation —
    pure generation time excluding network / queue / prefill; only rows with
    `attributes ? 'decode_ms'` count, and non-streaming fallbacks / empty
    streams are excluded by the denominator's NULLIF.

(The `llm_usage.latency_ms` p50/p95/max panel was removed 2026-08-05 per
user ruling — the latency percentiles live in the alert rules
(`../alerting/rules.yml`) instead. The `agent_restarted` stat and the
`process-restarts` chart were removed the same day — restart metrics retired from
the dashboard; the restart-spike alert rule (R3) still exists.)

### The observability core pack (core_metrics_observability.py)

The former `ava_observability` plugin pack (W18), promoted to core
2026-08-06: **LLM cost (USD)** (usage-time quotes from the versioned pricing
catalog, with a drift-lock test), **LLM errors**, **Turn success rate**, **Turn
duration (p50/p90/max)** (`percentile_cont` / `WITHIN GROUP` whitelist
extension), **Exec outcomes**, **Syntax fix triggers (by kind)**, **Agent
spawns (by source)**, **Agent lifecycle counts**, **SDK calls (Top 20)**
(`table`), **Per-agent LLM usage (Top 20)** (`table` — calls / in / out
tokens / cost per agent; the validator only allows `FROM events`, so the
table shows numeric agent ids, no label join), **Halt classes**, **Delivery
backlog**, **Event rate (events/s)** — 13 grafana panels, plus two
inspector-only per-agent metrics (`{{agent_id}}`): **Agent LLM cost (USD)**
and **Agent delivery backlog**.

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
  / `fieldConfig.defaults` (the generator's defaults win for keys the spec
  does not set). Core panels use these to keep their exact rendered look
  after migrating from hand-written JSON.
- `width` / `height` — explicit grid size (override the 6x4 stat / 12x7
  chart default).
- `thresholds` — absolute-threshold steps; an explicit empty list (`[]`)
  suppresses the default green base entirely (panels without any
  thresholds).

## Layout

Greedy 24-column grid, **no overlapping gridPos** (guaranteed by the
generator): stats default 6x4 (three per row) — the migrated core ops
panels keep their original 8-wide stat grid (three per row) via explicit
`width`/`height` — charts/tables default 12x7 (two per row), row headers
h=1 w=24. The core section starts at y=0 with the `core` row header; the
plugin block starts **exactly** at the core block's bottom (no gap row —
Grafana's vertical compacting would pull it up and the rendered layout
would disagree with the JSON).

Row pitch is 30px with an 8px gap; panels render at natural size and the
embed is **full-height** — the frontend iframe is sized to the dashboard's
fixed rendered height (see `EMBED_HEIGHT` in
`ui/web/src/app/insights/ops/page.tsx`) so the page scrolls and the iframe
has no inner scrollbar. **Do not add `autofitpanels` back**: on Grafana
13.1.1 it collapses every panel to a 30px title bar at embed widths, and
even where it scales instead it shrinks fonts below readability.

**TODO — EMBED_HEIGHT = 4820 (124 rows, unchanged across both architecture tiers as of 2026-08-06)**: the core section now
carries a row header (h=1) that the previous hand-written layout did not
have, so the total row count differs from the pre-migration 124 rows. After
the integration is verified, recompute `rows × 30px + (rows-1) × 8px +
116px chrome` and bump `EMBED_HEIGHT` in
`ui/web/src/app/insights/ops/page.tsx`.

## Plugin metrics — the plugin section

Plugins register metrics via `shared/plugin_metrics.py` (`MetricSpec`), and
the plugin section of `ava-ops-main.json` carries one row header per plugin
with its panels underneath; `ava-ops-plugins.json` is the plugin-only
subset. These JSONs were historically generated from the MetricSpec
registry by `scripts/gen_plugin_dashboard.py`; **the generator did not
survive the archive→public port** — until it is re-introduced (tracked
observability follow-up), a MetricSpec change must be reflected in the
dashboard JSONs by hand, keeping `ava-ops-plugins.json` a consistent subset
of `ava-ops-main.json`. Adding or removing a metric changes the dashboard's
total height — see the EMBED_HEIGHT TODO above.

### Writing a plugin metric

1. Add `metrics.py` to your plugin dir (e.g. `ava_builtins/plugins/<name>/metrics.py`).
2. Call `register_metric(MetricSpec(...))` at module top level — the plugin
   name is auto-filled from the import context; do not pass it.
3. The `query` template is **LogQL** (`query_type="logql"`) — the live event
   stream in Loki, the same dialect the core panels use (task #1280 / #180).
   Every template must select `{service_name="unknown_service"}` and pipeline
   `| json` before any event-field filter (event fields are structured
   metadata, NOT stream labels); `{event_name}` / `{category}` placeholders
   render as double-quoted literals, `{category_re}` renders unquoted for
   `category=~"{category_re}|log"`-style regexes, and the event
   stream/json/placeholder contract is validated by
   `shared/metrics_logql.py`. Stat panels run as instant queries over
   `[$__range]`; timeseries/barchart panels as range queries bucketed by
   `[$__interval]`; every count wraps in `sum(...)` (the unknown_service
   family has >500 streams over a day, and an unaggregated count_over_time
   hits Loki's per-query series cap).
4. `output` selects the surfaces: `["grafana"]` (panel in the generated
   dashboard), `["inspector"]` (the per-agent API surface, W13b — query may
   use the `{{agent_id}}` placeholder, rendered by the gateway to
   `agent_id = <n>`), or both. A query with `{{agent_id}}` cannot target
   grafana. The Grafana dashboard is the only display surface since
   2026-08-05 (user ruling): the frontend inspector no longer renders plugin
   metrics; the `/inspect/metrics` endpoint stays for API consumers.
5. Extending the whitelist (e.g. a new aggregate function) means editing the
   `_SQL_*` sets in `shared/plugin_metrics.py` + its tests — keep it minimal.

Shipped examples: `ava_builtins/plugins/ava_code/metrics.py` (syntax_fix
trend/stat, event_name=`syntax_fix`, category=`telemetry` since the
2026-08-05 event_name-category final convention),
`ava_builtins/plugins/ava_fleet/metrics.py` (task done rate dual-surface,
spawn frequency, and an inspector-only `{{agent_id}}` demo), and
`ava_builtins/plugins/ava_memory/metrics.py` (recall-filter runs / empty
ratio / error ratio). The former `ava_observability` example moved to the
core section (see `shared/core_metrics_observability.py`) — the repo's own
observability is core, not a plugin.

### Inspector surface (W13b) — the per-agent API

`output`-inspector metrics are served through `GET /api/agents/{id}/inspect/metrics`
(`gateway/routers/agent_inspect.py`). The frontend "Plugin metrics" inspector
section that consumed it was removed 2026-08-05 (user ruling: plugin metrics
display lives in Grafana only); the endpoint stays for API consumers. The
gateway builds the registry **in process**
(task #180 PR D: the generator that wrote `$AVA_HOME/state/plugin_metrics.json`
did not survive the archive->public port — the snapshot file is gone, and
`_load_plugin_metrics` imports every shipped plugin `metrics.py` under its
plugin context plus the core definition modules) — plugin metrics first,
then core, both rendered identically (same re-validation, macro
substitution, execution) — keeps metrics
whose `output` includes `inspector`, renders each template for the
requested agent (`{event_name}`/`{category}` -> double-quoted literals,
`{{agent_id}}` -> `agent_id="<n>"` for LogQL), **re-validates the rendered
query** (register-time validation does not protect against a spec drifting
after registration; anything that fails the template contract is a 500,
never executed), substitutes the Grafana time macros (`$__interval` -> `1h`,
`$__range` -> `24h` — the inspector has no dashboard time range, so the
gateway renders a fixed recent window, 24h in 1h buckets) and executes
LogQL queries against Loki (`gateway/loki_events.metric_range`); SQL
templates (the `core_live_agents` stat) still execute read-only against the
cluster's own Postgres, one savepoint per metric so a failing query never
poisons its siblings.

Response: one `PluginMetricResult` per inspector metric, in registration
order — `stat` metrics carry `value`; `timeseries`/`barchart` carry `series`
(`[{ts, value}]`, chronological). A metric whose query fails at runtime
carries `error` (the rest still render); a template failing the
re-validation -> 500 with the reason, `{{agent_id}}` template
without an agent id -> 400 (structurally unreachable via HTTP — the id is a
path param; the guard lives in the render helper). Unknown agent -> 404. The
frontend no longer polls this endpoint (section removed 2026-08-05).

## Syncing to the live Grafana

There is no sync: the LGTM Grafana container (host port 3003, `deploy/lgtm`
compose) mounts this directory read-only and its file provider reloads a
changed file within ~30s — editing here and checking out on the LGTM host
IS the deployment. The `uid` must stay `ava-ops-main` (the embed URL
depends on it); the datasource reference `{type: postgres, uid: "ops"}`
must match the provisioned `ops` datasource.

`refresh` is `1m` (dashboard-level) — the embed stays live without frontend
polling. Bucket width follows Grafana's `$__interval` (auto-computed from
the time range), so a window change via the URL also changes resolution.

## Import / update

Provisioning is wired by `dashboards.yml` in this directory (file provider,
`disableDeletion: true`, path `/etc/grafana/provisioning/dashboards` inside
the container). Update flow: edit the dashboard JSON here (keeping the uid
`ava-ops-main`) → land the change → the LGTM host's checkout advances and
the provisioner reloads within ~30s. Manual UI import is only for ad-hoc
inspection on a non-LGTM Grafana — pick the Postgres datasource for
`DS_AVA_PG` and keep the uid.

## Embed requirements (deployment-side, to verify)

Grafana is not deployed yet (2026-08-03); this dashboard is written against
the Grafana 8+ JSON schema but **untested against a live instance**. The
deployment (W1) must also provide:

- **Gateway reverse proxy** `/grafana/*` → the Grafana instance, behind the
  normal cluster auth (session cookie / bearer) so the iframe request is
  authorized like every other API route. The frontend only ever dials
  `{API_BASE}/grafana/...`.
- **`allow_embedding = true`** in Grafana config (default `false` sends
  `X-Frame-Options: deny`, which blocks the iframe).
- **Postgres datasource** reachable from Grafana with read access to
  `events` (and the `postgres` plugin enabled).
- Verify in the browser: embed renders at `/insights#ops` in light and dark
  theme, `&kiosk` hides Grafana chrome (sidemenu/topnav) while the dashboard
  timepicker stays usable, panels render at natural size, the iframe is
  full-height (no inner scrollbar; page scrolls; **EMBED_HEIGHT needs
  re-measuring — the core row gained a row**), and the time range / refresh interval change
  through Grafana's native controls.

## Alerting

**Live** (2026-08-04): Grafana Alerting rules, as code in
[`../alerting/rules.yml`](../alerting/rules.yml) — sixteen rules evaluated every minute
over Loki and Prometheus: WARNING+ERROR
spike, sse_drop/event_log_drop backlog, agent_restarted spike, LLM latency
p95, delivery_stalled fresh-backlog, events-table freshness, trace-disk
watermark, and the infrastructure five (issue #46 — host CPU, host memory,
per-volume disk watermark, Postgres connection saturation, Redis memory).
Those are joined by three collector-delivery rules:
current exporter queue pressure, new enqueue failures in a 5-minute window,
and a recently-seen host whose collector stopped reporting. Synced to
`~/.ava/grafana/provisioning/alerting/`; see
[`../alerting/README.md`](../alerting/README.md) for the rule
table, threshold calibration, and the notification-channel follow-up. The
reserved `ops_alert_rules` config store is
**retired** — Grafana is the
single rule store. Alerts land in the gateway's `/api/alerts`
(Task #1224: Alert separate from Notice, Alertmanager webhook shape).

Two 2026-08-05 user-ruling adjustments:

- **The alert-section list is unresolved-first** — resolved instances sort
  below unresolved ones even when unread and newer (`gateway/routers/alerts.py`),
  so a fresh resolution no longer buries an active incident.
- **The cluster health-probe is silent during deploys** — gateway/services
  going down mid-deploy is the expected transition (the rollout reports its
  own failures); `cli/commands/_cluster_health.py` no longer emits the
  edge alert while `deploy_in_flight` is true. A failure after the deploy
  window closes alerts normally. This was the recurring "cluster health"
  misfire — P1, resolved by the time the owner looked (4 instances on
  2026-08-05, every one during a deploy or the trace-incident window).
