"""An agent's explicit tuning values, for the endpoints that show what the agent runs with.

The gateway and the insights service hold no agent domain of the config, so they read only what
the agent pins; a value the agent does not pin falls to the model's registry default, as the
cluster-wide display does.
"""

from __future__ import annotations

from typing import Any

from psycopg_pool import ConnectionPool

from base.config.agent_pins import resolve_agent_config_pins
from base.host.env.agent_slices import ModelOverrides


def agent_overrides(
    config_overlay: dict[str, Any] | None, birth_config: dict[str, Any] | None
) -> ModelOverrides:
    """The overrides held by an agent's two stored config maps (overlay over birth config)."""
    return ModelOverrides.from_pins(resolve_agent_config_pins(config_overlay, birth_config))


def read_agent_overrides(pool: ConnectionPool[Any], agent_id: int) -> ModelOverrides:
    """`agent_overrides` of the agent's stored row; none for an agent without one."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        row = cur.fetchone()
    return agent_overrides(row[0] if row else None, row[1] if row else None)
