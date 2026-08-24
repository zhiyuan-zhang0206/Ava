"""Database boundary for permission-watcher IM notices."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import psycopg
from dotenv import dotenv_values

AGENT_ID = 312
NOTICE_PRIORITY = "P1"
ENV_PATH = Path.home() / ".ava" / ".env"

NOTICE_SQL = """
INSERT INTO agent_notices
(agent_id, local_id, title, content, priority, require_response, blocking, task_id)
VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1,
        %s, %s, %s, %s, %s, %s)
RETURNING id, local_id;
"""


class _Cursor(Protocol):
    def __enter__(self) -> _Cursor: ...

    def __exit__(self, *args: object) -> bool | None: ...

    def execute(self, sql: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[int, int] | None: ...


class _Connection(Protocol):
    def __enter__(self) -> _Connection: ...

    def __exit__(self, *args: object) -> bool | None: ...

    def cursor(self) -> _Cursor: ...


ConnectFactory = Callable[..., _Connection]


def insert_notice(
    db_url: str,
    title: str,
    content: str,
    *,
    connect: ConnectFactory | None = None,
) -> tuple[int, int]:
    """Insert one FYI notice for the machine resource-monitor role."""
    factory = connect or psycopg.connect
    with factory(db_url, connect_timeout=5) as connection, connection.cursor() as cursor:
        params: tuple[object, ...] = (
            AGENT_ID,
            AGENT_ID,
            title,
            content,
            NOTICE_PRIORITY,
            False,
            False,
            None,
        )
        cursor.execute(NOTICE_SQL, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("permission notice insert returned no row")
    return row


def read_db_url(env_path: Path = ENV_PATH) -> str:
    """Read the local PgBouncer URL from the prod unit's explicit env file."""
    value = dotenv_values(env_path).get("AVA_DB_URL")
    if not value:
        raise RuntimeError(f"AVA_DB_URL is missing from {env_path}")
    return value
