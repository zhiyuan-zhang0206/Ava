"""Gateway HTTP API contract types shared with the CLI thin clients.

These response models are produced by the gateway (registered on its FastAPI
routes, so they appear in the OpenAPI spec / `types-generated.ts` under their
class name) and consumed by `cli/` thin clients that hit those same endpoints.
The import layering `shared < ... < {gateway, cli}` bars `cli` from importing
`gateway`, so a contract both sides speak lives here at the bottom layer:
`gateway.schemas` re-exports each name (the OpenAPI schema name is the class
`__name__`, unchanged by the move), and `cli` validates responses against these
directly instead of hand-unpacking dicts.

Only the subset the CLI actually decodes lives here — gateway-only response
models (ClusterPanel, SystemStatus, ServiceItem, ...) stay in `gateway.schemas`.
`strict_decode.py` supplies stdlib-only field checks for durable JSON documents.

Import `config`, `contracts`, `op_envelope`, `status`, and `strict_decode` directly.
Nothing is re-exported here: every submodule import executes this initializer.
Settings-free consumers must not inherit unrelated chains such as
`config.py -> shared.config_registry` or `status.py -> DB`.
"""
