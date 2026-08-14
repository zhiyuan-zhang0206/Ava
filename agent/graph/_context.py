"""Backward-compat re-export — AvaContext + agent_id_from_config moved to
`shared/context.py`.

`shared/context.py` is the canonical home. Non-graph entry points (gateway
lifespan, daemon `run()`, cli, eval driver) will also build AvaContext, so
the dataclass lives at the project root rather than under `agent/graph/`.

This module remains as a re-export only so existing
`from agent.graph._context import AvaContext` callers (plugins, tests) don't
break. New code should import directly from `shared.context`.
"""

from shared.context import AvaContext as AvaContext
from shared.context import agent_id_from_config as agent_id_from_config
