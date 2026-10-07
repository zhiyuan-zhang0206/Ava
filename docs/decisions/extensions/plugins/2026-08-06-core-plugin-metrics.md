---
type: decision
title: Core + Plugin two-tier metrics architecture
description: User ruling: metrics are split into two tiers — the core section (row header "core") first, the plugin section showing plugin names. Core metrics move into the registration system (shared/core_metrics.py, same SQL validation as plugins); the 16 hand-written panels and the ava_observability package all migrate in; the generator emits a single dashboard (ava-ops-main.json = core section + plugin section) + plugin middleware (ava-ops-plugins.json) + a two-section registry snapshot.
tags: [observability, grafana, metrics, dashboard, plugin]
date: 2026-08-06
status: accepted
---

# Core + Plugin two-tier metrics architecture

## Background

After Grafana single-iframe embedding, `/insights#ops` loads a single dashboard (`dashboards/ops/ava-ops-main.json`), with plugin metric panels merged into the same file at ids >= 1000. At that point two architectures coexisted: **one registration-based (plugins) + one hand-written (core panels)**:

- **Plugin metrics** (W13/W18): `scripts/gen_plugin_dashboard.py` collects each plugin's `register_metric()` from its `metrics.py` and generates `ava-ops-plugins.json`, then merges it into `ava-ops-main.json` — already registration-based.
- **Core panels**: the 16 ops panels (LLM usage/errors, throughput, TPS, cache hits, SSE backlog, event health…) were still **hand-written inside ava-ops-main.json** — no registration, no validation, a second source of truth next to the generator.
- **ava_observability positioning**: the general observability metrics package (W18) lives under the plugin directory `ava_builtins/plugins/ava_observability/` — the user's objection: this is the repo's own observability, not a business plugin — why does it show up as a plugin?

The user ruled 2026-08-06 on the two-tier architecture and its display rules:

1. Metrics split into **core / plugin** tiers;
2. The **core section comes first**, its row header shows `"core"`;
3. The **plugin section shows plugin names** (one row per plugin);
4. observability is the repo's own observability, **not a plugin** — it should be promoted from plugin to core.

## Decision

1. **Core registration system** (`shared/core_metrics.py`): `register_core_metric()` / `collect_core_metrics()` / `export_core_registry()`. Registration pins the `plugin` field to `"core"` (dashboard row header and display name = "core"); it shares the **same SQL template safety validation as plugin metrics** (`validate_spec_sql`: whitelisted functions, single SELECT over `events`, `{{agent_id}}` ↔ output rules, per-target validation) — core is not a privileged tier exempt from checks. Core and plugin registries do not interfere (a duplicate name raises `DuplicateMetric` in each).
2. **Definition modules**: core metric definitions live in `shared/core_metrics_panels.py` (all 16 original hand-written panels migrated in) + `shared/core_metrics_observability.py` (the former `ava_observability` plugin package migrated in); `collect_core_metrics()` imports them in that order and tolerates missing modules (partial checkouts / test environments do not crash; the core section renders empty).
3. **Single generator entry** (`scripts/gen_plugin_dashboard.py`) produces four things:
   - `dashboards/ops/ava-ops-main.json` — the full dashboard: **core section (row header "core") first + one row per plugin** (the plugin block follows the core block immediately, no gap rows);
   - `dashboards/ops/ava-ops-plugins.json` — **plugin middleware** (plugin panels only, no longer embedded by the frontend; kept for tests / standalone use);
   - registry snapshot `$AVA_HOME/state/plugin_metrics.json` — **two sections**: `metrics` (plugins) + `core_metrics` (core), query templates exported verbatim;
   - provisioning sync: **both JSONs are copied** to `~/.ava/grafana/provisioning/dashboards/` (controlled by `--no-sync` / `--provisioning-dir`).
4. **Panel id ranges**: core panel ids < 1000 (1..n, the core row header id = 900); plugin panel ids >= 1000. The two ranges never collide.
5. **MetricSpec extension** (`shared/plugin_metrics.py`): add `targets` (multi-series refIds B/C/...), `options` / `custom` / `field_defaults` (panel appearance overrides), `width` / `height` (explicit grid size), `thresholds=[]` (suppress the default green background) — so the 16 hand-written panels keep their per-panel rendering after migrating into the registration system; generator defaults apply on keys the spec does not set.
6. **Display rules landed**: the generator's core section = one row header `"core"` + all core panels (registration order, greedy 24-column layout); the plugin section = one row header per plugin (plugin name) + that plugin's panels.
7. **observability promoted to core**: the `ava_observability` metric definitions move from the plugin package into `shared/core_metrics_observability.py` and no longer appear as a plugin (the plugin directory's metrics responsibility ends).
8. **Two-section inspector snapshot**: the gateway (`gateway/routers/_plugin_metrics.py`) merges the `metrics` + `core_metrics` sections when reading the snapshot, through the same render / re-validate / execute path — both tiers behave identically on `/api/agents/{id}/inspect/metrics`.
9. **Frontend unchanged**: still a single iframe (#875) loading `ava-ops-main`; `EMBED_HEIGHT` needs re-measuring (the core row header adds one row; total rows differ from the old 124).

## Alternatives rejected

- **observability stays a plugin**: the user explicitly objected — the repo's own observability is not a business plugin; under a plugin name it renders as an ordinary plugin row, semantically wrong. Rejected.
- **core panels stay hand-written JSON**: coexisting with the registration system = two sources of truth that inevitably drift (validation, layout, docs each in one copy); all 16 panels have migrated into the registration system, hand-written JSON is no longer maintained. Rejected.
- **core reuses the plugin registration channel** (treating core as a special plugin): the display name would be polluted by plugin semantics, and core should not require a PluginContext / plugin directory; a separate registry + a `plugin="core"` field is more direct. Rejected (landed as the standalone `register_core_metric`).

## Consequences

- **Changing a panel = changing the definition module + re-running the generator**: `ava-ops-main.json` / `ava-ops-plugins.json` are generated artifacts, no longer hand-edited; before committing you must re-run `python scripts/gen_plugin_dashboard.py` and sync the provisioning copies (provisioning is the runtime authority; both JSONs must be copied).
- **Id range convention**: core < 1000 (core row = 900), plugins >= 1000 — future metrics follow automatically, no manual allocation.
- **The old `ava-ops-plugins.json` role changes**: from "frontend embed target" downgraded to "plugin middleware"; `PLUGINS_EMBED_HEIGHT` has been deleted, the frontend has a single iframe only.
- **EMBED_HEIGHT**: adding the core row header changes the total row count; re-measure after integration (rows × 30px + (rows-1) × 8px + 116px chrome), marked as a TODO in `dashboards/ops/README.md`.
- **Docs**: `dashboards/ops/README.md` rewritten for the two-tier architecture; this decision record; historical records stay as they are (decisions are not rewritten).
