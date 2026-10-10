"""Backup process roots retain their captured image and one database gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from base.config import settings
from base.db import Database
from base.deploy.schema import migrations
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.backup.scheduler import daemon, worker


class _Runner:
    def run(self, work: Coroutine[Any, Any, None]) -> None:
        asyncio.run(work)


def test_scheduler_shares_captured_image_with_health_and_lazy_log_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = LoadedCommit(tmp_path, "captured-scheduler")
    capture = Mock(return_value=image)
    create = Mock(return_value=Mock(spec=Database))
    pipeline = Mock(return_value=object())
    logging = Mock()
    count = Mock(return_value=7)
    exit_process = Mock()
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(Database, "from_settings", create)
    monkeypatch.setattr(daemon, "build_pipeline", pipeline)
    monkeypatch.setattr(daemon, "init_gateway_process", logging)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", Mock())
    monkeypatch.setattr(daemon, "_remove_pidfile", Mock())
    monkeypatch.setattr(daemon, "_hard_exit", exit_process)
    monkeypatch.setattr(daemon, "asyncio", SimpleNamespace(Runner=_Runner))
    monkeypatch.setattr(migrations, "assert_schema_current", Mock())
    monkeypatch.setattr(code_version, "first_parent_count", count)

    async def run(*, config: Any, image: LoadedCommit) -> None:
        assert image is capture.return_value

    monkeypatch.setattr(daemon, "run", run)
    daemon.main()
    capture.assert_called_once_with()
    create.assert_not_called()
    count.assert_not_called()
    assert logging.call_args.kwargs["image"] is image
    assert logging.call_args.kwargs["producer"]() is pipeline.return_value
    factory = pipeline.call_args.kwargs["database"]
    factory()
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert gates[0] is gates[1]
    assert gates[0].application_name() == "ava:pg_backup:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    exit_process.assert_called_once_with(0)


def test_worker_shares_one_gate_with_work_log_and_scratch_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = LoadedCommit(tmp_path, "captured-worker")
    capture = Mock(return_value=image)
    ordinary = Mock(return_value=Mock(spec=Database))
    create = Mock(side_effect=Database)
    create.from_settings = ordinary
    pipeline = Mock(return_value=object())
    logging = Mock()
    received: list[Callable[[], Database]] = []
    count = Mock(return_value=7)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    output = tmp_path / "result.json"
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(worker, "worker_request", Mock(return_value=({"kind": "restore"}, output)))
    monkeypatch.setattr(worker, "Database", create)
    monkeypatch.setattr(worker, "build_pipeline", pipeline)
    monkeypatch.setattr("base.log.init_gateway_process", logging)
    publish = Mock()
    monkeypatch.setattr(worker, "publish_result", publish)

    def execute(
        request: dict[str, object],
        work: Path,
        *,
        config: Any,
        database: Callable[[], Database],
        database_for_url: Callable[[str], Database],
    ) -> dict[str, object]:
        received.append(database)
        database()
        database_for_url("postgresql://scratch.invalid/one")
        monkeypatch.setattr(settings.data_plane, "db_sslmode", "verify-full")
        database_for_url("postgresql://scratch.invalid/two")
        return {"restored": True}

    monkeypatch.setattr(worker, "_execute", execute)
    worker.main()
    capture.assert_called_once_with()
    assert pipeline.call_args.kwargs["database"] is received[0]
    assert logging.call_args.kwargs["image"] is image
    gate = ordinary.call_args.kwargs["gate"]
    assert all(call.kwargs["gate"] is gate for call in create.call_args_list)
    assert gate.application_name() == "ava:pg-backup-worker:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    configs = [call.args[0] for call in create.call_args_list]
    assert [config.db_url for config in configs] == [
        "postgresql://scratch.invalid/one",
        "postgresql://scratch.invalid/two",
    ]
    assert configs[1].db_sslmode == "verify-full"
    assert configs[0].db_pool_min_size == configs[1].db_pool_min_size
    assert configs[0].pgbouncer_enabled == configs[1].pgbouncer_enabled
    publish.assert_called_once_with(output, {"restored": True})


async def test_scheduler_health_uses_entry_image_and_preserves_loop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import ConfigBoot

    image = LoadedCommit(Path(), None)
    failure = RuntimeError("original backup loop failure")
    health = object()
    start = AsyncMock(return_value=health)
    stop = AsyncMock()
    monkeypatch.setattr(daemon, "_is_running", Mock(return_value=False))
    monkeypatch.setattr(daemon, "_write_pidfile", Mock())
    remove = Mock()
    monkeypatch.setattr(daemon, "_remove_pidfile", remove)
    monkeypatch.setattr(daemon, "start_health_server", start)
    monkeypatch.setattr(daemon, "stop_health_server", stop)
    monkeypatch.setattr(daemon, "_backup_loop", AsyncMock(side_effect=failure))

    with pytest.raises(RuntimeError) as raised:
        await daemon.run(config=ConfigBoot(), image=image)

    assert raised.value is failure
    assert start.call_args.kwargs["image"] is image
    assert start.call_args.kwargs["components"]()[0]["name"] == "backup"
    stop.assert_awaited_once_with(health)
    remove.assert_called_once_with()
