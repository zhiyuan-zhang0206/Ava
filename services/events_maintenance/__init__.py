"""Events-maintenance daemon — gateway-owned event-stream upkeep.

The hourly pass upserts the Since-Birth rollups (agent_metrics_daily /
agent_model_tokens_daily — the durable token+cost ledger, aggregated from Loki
since the LGTM cutover; `services.events_maintenance.rollup`), repairs
pre-retention gaps from the 90-day filtered JSONL replay source
(`services.events_maintenance.jsonl_replay`), runs the blob vacuum, and samples
checkpoint table sizes. The checkpoint trim opt-in was retired on 2026-09-30
under the never-delete ruling; the retained reaper implementation is not
scheduled. A five-minute resolution slice reads immutable Loki event classes,
combines them with `event_dismissals`, and publishes unresolved + dismissed
warning/error gauges (`services.events_maintenance.resolution`); the gateway
stats dashboard reuses the same class arithmetic for its selected window. The PG `events` archive
maintenance slices (partitions / retention / table retention / reindex) were
removed with the task #1281/#1823 cleanup — the table was dropped.
See `services.events_maintenance.daemon` for the poll loops.
"""
