"""agent spawn + lifecycle response rows.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from shared.agent_snapshot import AgentListCompact, AgentListSummary, AgentSnapshot


class AgentSummary(AgentListSummary):
    """One row of ``GET /api/agents?fields=summary`` for roster consumers."""


class AgentCompact(AgentListCompact):
    """One row of the legacy narrow ``GET /api/agents?fields=compact`` projection."""


class AgentRow(AgentSnapshot):
    """One row of GET /api/agents — full state snapshot of the `agents` table.

    Identical schema to `AgentSnapshot` (the SSE-side type); subclassed
    rather than aliased so OpenAPI keeps the historical name `AgentRow` for
    the generated frontend types. `last_active_at` is the real-activity clock
    (agents_meta), `last_inbound_at` the latest inbound (issue #183).
    """
