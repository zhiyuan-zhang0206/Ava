"""Observe committed task notes while preserving readable call-style assertions."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock

import psycopg

from base.db import fetch_one


class TaskNotes:
    """Query durable rows, rather than intercepting an HTTP notification call."""

    def __init__(self, conn: psycopg.Connection, after: int) -> None:
        self.conn = conn
        self.after = after

    def capture(self) -> Mock:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, content, payload FROM inbound_messages "
                "WHERE id > %s AND kind='system_note' ORDER BY id",
                (self.after,),
            )
            rows = cur.fetchall()
        observed = Mock()
        for recipient, content, payload in rows:
            kwargs: dict[str, object] = {"resurrect": payload["delivery_resurrect"]}
            if "task_id" in payload:
                kwargs["task_id"] = payload["task_id"]
            observed(recipient, content, **kwargs)
        return observed

    @property
    def call_args(self) -> Any:
        return self.capture().call_args

    @property
    def call_args_list(self) -> Any:
        return self.capture().call_args_list

    @property
    def call_count(self) -> int:
        return self.capture().call_count

    def assert_called_once(self) -> None:
        self.capture().assert_called_once()

    def assert_not_called(self) -> None:
        self.capture().assert_not_called()


@contextmanager
def record_notes(conn: psycopg.Connection) -> Generator[TaskNotes]:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM inbound_messages")
        after = int(fetch_one(cur, "note baseline")[0])
    yield TaskNotes(conn, after)
