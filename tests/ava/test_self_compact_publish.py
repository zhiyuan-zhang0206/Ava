"""`ava.self.compact()` must reach its wake + `_SystemHalt` even if the
CompactRequest publish fails — a redis outage must not interrupt this lifecycle
exit (it used to be a bare `ava.REDIS.publish` that would raise past the wake).
"""

from __future__ import annotations

import psycopg
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import ava
from shared import redis_client
from shared.lifecycle import _SystemHalt
from tests.conftest import spawn_agent


class _BoomSyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def publish(self, _channel: str, _payload: str) -> int:
        raise self._exc

    def close(self) -> None:
        pass


def test_compact_survives_publish_failure(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throwing redis on the CompactRequest publish must not stop compact from
    committing its compact_summary inbound and raising _SystemHalt."""
    ava._boot._agent_id = spawn_agent()  # self identity

    # Only the CompactRequest publish (publish_best_effort_sync → sync_redis) is
    # broken; the self-inbound wake uses ava.REDIS directly and is already
    # never-raise, so leave the session redis real for it.
    monkeypatch.setattr(
        redis_client,
        "sync_redis",
        lambda **_: _BoomSyncClient(RedisConnectionError("down")),  # pyright: ignore[reportUnknownArgumentType]
    )

    with pytest.raises(_SystemHalt):
        ava.self.compact("Requests: (none)\nProgress: done\n")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "compact_summary"
