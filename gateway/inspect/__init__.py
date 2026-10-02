"""Inspector read surface for one agent: routes, response models, lineage +
plugin extension resolution, and the cache/live/metrics reads behind them.

Package door — no imports, no re-exports; callers use `gateway.inspect.router`
(mounted in `gateway/app.py`), `gateway.inspect.schemas` (re-exported from
`gateway.schemas`), and `gateway.inspect.neighbors` (also used by
`gateway/agents/router.py`'s born-chain endpoint). Modules:

  - `router.py`          — mounted endpoints: /inspect/live, /inspect/statistics,
                            /neighbors, /inspect/metrics, /inspect/widgets
  - `schemas.py`          — response models + the persisted-statistics evidence/window types
  - `neighbors.py`        — neighbor graph + immutable birth-chain computation
  - `_cache.py`           — bounded TTL + single-flight query cache with load admission
  - `_live.py`            — fresh agents_meta/notice projections for /inspect/live
  - `_metrics.py`         — SQL-only persisted statistics over observations + historical days
  - `_metrics_health.py`  — coverage verdicts: background log + alert episodes
  - `_plugin_widgets.py`  — plugin inspector-widget loading + per-agent resolution
  - `_plugin_metrics.py`  — plugin metric registry + per-agent execution
"""
