"""Home-machine read of the shared user context, through the existing ops server."""

from psycopg_pool import ConnectionPool

from base.agents.to_user import ToUserDocument, ToUserFormat, read_document
from base.config.agent_pins import resolve_agent_config_pins
from base.paths import workspace_dir_readonly


def read_to_user(
    pool: ConnectionPool, agent_id: int, machine: str, *, default_format: ToUserFormat
) -> ToUserDocument:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, config_overlay, birth_config FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] != machine:
        raise ValueError("To user can only be read on the agent's home machine")
    pins = resolve_agent_config_pins(row[1], row[2])
    format = pins.get("to_user_format", default_format)
    return read_document(workspace_dir_readonly(agent_id), format)
