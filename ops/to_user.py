from pathlib import Path

from psycopg_pool import ConnectionPool

from base.agents.to_user import ToUserDocument, read_document
from base.paths import workspace_dir_readonly


def read_to_user(
    pool: ConnectionPool, agent_id: int, machine: str, *, references: list[str]
) -> ToUserDocument:
    return read_document(owned_workspace(pool, agent_id, machine), references=references)


def owned_workspace(pool: ConnectionPool, agent_id: int, machine: str) -> Path:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None or row[0] != machine:
        raise ValueError("To user can only be read on the agent's home machine")
    return workspace_dir_readonly(agent_id)
