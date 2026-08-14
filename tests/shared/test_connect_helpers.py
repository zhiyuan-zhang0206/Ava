"""`shared.db` / `shared.redis_client` connection helpers read settings once
and hand back a working connection / pool / sync client, so call sites stop
hand-writing `psycopg.connect(settings.data_plane.db_url)` and
`redis.Redis.from_url(settings.data_plane.redis_url)`. These pin that the helpers read the
live settings URL (the conftest testcontainer) and pass options through.
"""

from __future__ import annotations

from shared import db
from shared.redis_client import sync_redis


def test_connect_runs_a_query() -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_connect_autocommit_passthrough() -> None:
    with db.connect(autocommit=True) as conn:
        assert conn.autocommit is True


def test_connect_defaults_to_manual_commit() -> None:
    with db.connect() as conn:
        assert conn.autocommit is False


def test_pool_hands_out_working_connections() -> None:
    pool = db.pool(min_size=1, max_size=2)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        pool.close()


def test_sync_redis_ping() -> None:
    client = sync_redis()
    try:
        assert client.ping() is True  # pyright: ignore[reportUnknownMemberType]
    finally:
        client.close()


def test_sync_redis_decode_responses_passthrough() -> None:
    client = sync_redis(decode_responses=True)
    try:
        client.set("ava:test:connect-helper", "v")
        assert client.get("ava:test:connect-helper") == "v"  # str, not bytes
    finally:
        client.delete("ava:test:connect-helper")
        client.close()
