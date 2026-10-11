"""Gateway HTTP server layer.

`ops/` is the home for shared operations code (Spec / Status / controllers /
cluster RPC); `gateway/` is the FastAPI HTTP surface over it. The gateway's
request/response schemas live in `gateway.schemas` (this package's `schemas/`
subpackage); the RPC-shared wire types the agent-runner also consumes live one
layer down in `ops.rpc_schemas` (import layering: base < ops < gateway).

"""
