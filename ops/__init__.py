"""Shared operations package — used by both gateway HTTP server and agent-runner services.

This package was extracted from `gateway/` to decouple the two concerns:
- `gateway/` — HTTP server only (app.py, routers/, sse.py, __main__.py, pages.py)
- `ops/` — shared operations (agent lifecycle, cluster ops, config, schemas, RPC)

Both the gateway HTTP handlers and the agent-runner ops server import from here.

The ops layer's planned end state (identity, Drain, CronJob) is tracked in
`ops/ops-module.md` — the doc co-location ruling keeps the module's plan beside
the module it plans.
"""
