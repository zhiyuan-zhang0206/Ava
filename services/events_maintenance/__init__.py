"""Events-maintenance daemon — gateway-owned unified event-stream upkeep.

Each pass keeps the event tables' structure current: it ensures the
current/next month partitions exist (`services.events_maintenance.partitions`),
applies the unified `events` retention policy (`services.events_maintenance.retention`
— drop/prune expired categories), and upserts the Since-Birth
rollups (agent_metrics_daily / agent_model_tokens_daily — the durable
token+cost ledger, aggregated from Loki since the LGTM cutover;
`services.events_maintenance.rollup`). See
`services.events_maintenance.daemon` for the poll loop.
"""
