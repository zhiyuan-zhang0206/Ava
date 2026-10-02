"""Wire models of the gateway routers that live in `gateway/routers/`, one family
module each — the package door re-exports nothing: import the family module
that defines a model (`from gateway.schemas.tasks import TaskRow`).

A feature package owns its own wire models beside its routes
(`gateway.agents.schemas`, `gateway.alerts.schemas`, `gateway.cluster.schemas`,
`gateway.events.schemas`, `gateway.extensions.schemas`,
`gateway.inspect.schemas`, `gateway.run_timeline.schemas`). What stays here:

  - one family per `gateway/routers/` module (`commands`, `fleet_graph`,
    `frontend_telemetry`, `memory`, `pages`, `shell`, `tasks`, `uploads`,
    `user_settings`, `work_failed`);
  - vocabulary several packages share: `errors` (the RFC 9457-style error
    envelope every route answers with), `models` (the model catalog and the
    cluster default model), `stats` (the whitelisted `?hours=` window).

The gateway<->runner RPC-shared types live one layer down in `ops.rpc_schemas`,
and the response models the `cli` thin clients also decode (`MachineStatus`, the
`Config*` family) in `base.api_contracts` (import layering: base < ops <
gateway).
"""
