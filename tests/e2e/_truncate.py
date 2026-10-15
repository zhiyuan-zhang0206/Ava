from __future__ import annotations

from typing import Any, LiteralString

import psycopg


def truncate_with_deadlock_retry(conn: psycopg.Connection[Any], statement: LiteralString) -> None:
    for attempt in range(3):
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
            conn.commit()
            return
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise
            conn.rollback()
