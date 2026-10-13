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
from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL, UNPROVISIONED_REDIS_URL
from tests.fixtures.unit.static_environment import static_mode


def test_process_environment_keeps_its_home_and_data_plane_boundary(
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
    process_config: object,
) -> None:
    assert Path(os.environ["AVA_HOME"]) != Path.home() / ".ava"
    assert not os_jobs_enabled(enabled_reader=lambda: settings.general.os_jobs_enabled)
    if not static_mode(pytestconfig):
        request.getfixturevalue("postgres_access")
        request.getfixturevalue("db_url")
        request.getfixturevalue("redis_access")
        request.getfixturevalue("redis_url")
    db = conninfo_to_dict(settings.data_plane.db_url)
    if static_mode(pytestconfig):
        from tests import _containers

        expected = conninfo_to_dict(UNPROVISIONED_DB_URL)
        assert {key: db[key] for key in expected} == expected
        assert settings.data_plane.redis_url == UNPROVISIONED_REDIS_URL
        for fixture in ("db_url", "redis_url"):
            with pytest.raises(pytest.fail.Exception, match="Static tests cannot request"):
                request.getfixturevalue(fixture)
        with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
            _containers.postgres(machine_name=lambda: pytest.fail("Static identity must stay cold"))
        with pytest.raises(pytest.fail.Exception, match="Static tests cannot use"):
            _containers.redis_server()
    else:
        assert db["dbname"] != "unprovisioned"
        assert settings.data_plane.redis_url != UNPROVISIONED_REDIS_URL


def test_sync_data_plane_calls_follow_the_process_environment(
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
    process_config: object,
) -> None:
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
        request.getfixturevalue("postgres_access")
        request.getfixturevalue("db_url")
        request.getfixturevalue("redis_access")
        request.getfixturevalue("redis_url")
        with psycopg.connect(settings.data_plane.db_url) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
        with redis.Redis.from_url(settings.data_plane.redis_url) as client:
            assert client.ping()


async def test_async_data_plane_calls_follow_the_process_environment(
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
    process_config: object,
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
        request.getfixturevalue("postgres_access")
        request.getfixturevalue("db_url")
        request.getfixturevalue("redis_access")
        request.getfixturevalue("redis_url")
        async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as connection:
            assert await (await connection.execute("SELECT 1")).fetchone() == (1,)
        async with redis.asyncio.Redis.from_url(settings.data_plane.redis_url) as client:
            assert await client.ping()
