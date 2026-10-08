"""The process lane must isolate native effects without weakening native runs."""

import os
from pathlib import Path

import psycopg
import pytest
import redis
import redis.asyncio
from psycopg.conninfo import conninfo_to_dict

from base.config import settings
from base.host.system.cron import os_jobs_enabled
from tests import _containers
from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL, UNPROVISIONED_REDIS_URL
from tests.fixtures.static_environment import static_mode


def test_process_environment_keeps_its_home_and_data_plane_boundary(
    pytestconfig: pytest.Config,
) -> None:
    assert Path(os.environ["AVA_HOME"]) != Path.home() / ".ava"
    assert not os_jobs_enabled()
    db = conninfo_to_dict(settings.data_plane.db_url)
    if static_mode(pytestconfig):
        expected = conninfo_to_dict(UNPROVISIONED_DB_URL)
        assert {key: db[key] for key in expected} == expected
        assert settings.data_plane.redis_url == UNPROVISIONED_REDIS_URL
        for provision in (_containers.postgres, _containers.redis_server):
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
                provision()
    else:
        assert db["dbname"] != "unprovisioned"
        assert settings.data_plane.redis_url != UNPROVISIONED_REDIS_URL


def test_sync_data_plane_calls_follow_the_process_environment(pytestconfig: pytest.Config) -> None:
    if static_mode(pytestconfig):
        with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
            psycopg.connect(settings.data_plane.db_url)
        with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
            psycopg.Connection.connect(settings.data_plane.db_url)
        with redis.Redis.from_url(settings.data_plane.redis_url) as client:
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
                client.ping()
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
                pipeline = client.pipeline()
                pipeline.ping()
                pipeline.execute()
    else:
        with psycopg.connect(settings.data_plane.db_url) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
        with redis.Redis.from_url(settings.data_plane.redis_url) as client:
            assert client.ping()


async def test_async_data_plane_calls_follow_the_process_environment(
    pytestconfig: pytest.Config,
) -> None:
    if static_mode(pytestconfig):
        with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
            await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
        async with redis.asyncio.Redis.from_url(settings.data_plane.redis_url) as client:
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
                await client.ping()
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
                pipeline = client.pipeline()
                pipeline.ping()
                await pipeline.execute()
    else:
        async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as connection:
            assert await (await connection.execute("SELECT 1")).fetchone() == (1,)
        async with redis.asyncio.Redis.from_url(settings.data_plane.redis_url) as client:
            assert await client.ping()
