"""Events-maintenance daemon — gateway-owned event-stream upkeep.

The hourly pass recovers the observations the live projection missed, replays this machine's
JSONL mirror into `telemetry_events`, recomputes the day-grain rollups of the last closed days
(agent_metrics_daily / agent_model_tokens_daily — the durable token+cost ledger;
`services.upkeep.events_maintenance.rollup`), runs the blob vacuum, and samples
checkpoint table sizes. The checkpoint trim opt-in was retired on 2026-09-30
under the never-delete ruling; the retained reaper implementation is not
scheduled. A five-minute resolution slice reads the event classes in `telemetry_events`,
combines them with `event_dismissals`, and publishes unresolved + dismissed
warning/error gauges (`services.upkeep.events_maintenance.resolution`); the gateway's alert-class
surface reads the same classes and dismissal match for its selected window. The PG `events` archive
maintenance slices (partitions / retention / table retention / reindex) were
removed with the task #1281/#1823 cleanup — the table was dropped.
See `services.upkeep.events_maintenance.daemon` for the poll loops.
"""
