"""agent spawn + lifecycle response rows.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from shared.agent_snapshot import AgentSnapshot


class AgentRow(AgentSnapshot):
    """One row of GET /api/agents — full state snapshot of the `agents` table.

    Identical schema to `AgentSnapshot` (the SSE-side type); subclassed
    rather than aliased so OpenAPI keeps the historical name `AgentRow` for
    the generated frontend types.
    """
