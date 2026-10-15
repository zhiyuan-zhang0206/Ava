import inspect
from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from services.agent_runner.agent_ops import boot


def test_pool_and_bus_read_the_same_live_owner(database: Database, tmp_path: Path) -> None:
    config = snapshot_process_config()
    runtime = config.ensure_eager()
    authority = ConfigAuthority(runtime, runtime, tmp_path / ".env")
    calls: list[Database] = []

    def owned_database() -> Database:
        calls.append(database)
        return database

    config.set_field("ops_concurrency", 3)
    config.set_field("events_channel", "first-owned-channel")
    first_db, first_bus = boot.ops_handles(database=owned_database, authority=authority)
    first_pool = boot.open_db_pool(database=owned_database, authority=authority)
    try:
        assert first_db is database
        assert first_bus.channel == "first-owned-channel"
        assert first_pool.max_size == 5
        config.set_field("ops_concurrency", 5)
        config.set_field("events_channel", "second-owned-channel")
        second_db, second_bus = boot.ops_handles(database=owned_database, authority=authority)
        second_pool = boot.open_db_pool(database=owned_database, authority=authority)
        try:
            assert second_db is database
            assert second_bus.channel == "second-owned-channel"
            assert second_pool.max_size == 7
        finally:
            second_pool.close()
        assert first_bus.channel == "first-owned-channel"
        assert first_pool.max_size == 5
        assert calls == [database, database, database, database]
    finally:
        first_pool.close()


def test_ops_endpoint_requires_an_owner_and_runtime(
    process_config: ConfigBoot, tmp_path: Path
) -> None:
    first = boot.endpoint(process_config, runtime=tmp_path / "first")
    second = boot.endpoint(process_config, runtime=tmp_path / "second")
    assert first.name == second.name == "ops"
    assert first.health_port == second.health_port
    assert first.pidfile == tmp_path / "first" / "ops.pid"
    assert second.pidfile == tmp_path / "second" / "ops.pid"
    with pytest.raises(TypeError, match="runtime"):
        inspect.signature(boot.endpoint).bind(process_config)
