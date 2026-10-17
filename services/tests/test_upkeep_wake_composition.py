"""Four service entries share one captured image and gate with their log producer."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from base.config import ConfigBoot
from base.db import Database
from base.deploy.schema import migrations
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.upkeep.events_maintenance import daemon as events_maintenance_daemon
from services.upkeep.ttl_reaper import daemon as ttl_reaper_daemon
from services.wake.delivery_watchdog import daemon as delivery_watchdog_daemon
from services.wake.schedule_manager import daemon as schedule_manager_daemon

_ENTRIES = (
    ttl_reaper_daemon,
    events_maintenance_daemon,
    delivery_watchdog_daemon,
    schedule_manager_daemon,
)


class _Runner:
    def run(self, work: Coroutine[Any, Any, None]) -> None:
        asyncio.run(work)


@pytest.fixture(params=_ENTRIES, ids=lambda module: module.__name__)
def entry(request: pytest.FixtureRequest) -> Iterator[ModuleType]:
    """Restore this entry test's environment delivery after its real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield request.param
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


def _prepare_entry(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, image: LoadedCommit
) -> tuple[Mock, Mock, Mock, Mock, Mock]:
    capture = Mock(return_value=image)
    create = Mock(return_value=Mock(spec=Database))
    pipeline = Mock(return_value=object())
    logging = Mock()
    exit_process = Mock()
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(entry, "Database", create)
    monkeypatch.setattr(entry, "build_pipeline", pipeline)
    monkeypatch.setattr(entry, "init_gateway_process", logging)
    monkeypatch.setattr(entry, "install_graceful_shutdown", Mock())
    monkeypatch.setattr(entry, "asyncio", SimpleNamespace(Runner=_Runner))
    monkeypatch.setattr(entry, "_hard_exit", exit_process)
    monkeypatch.setattr(migrations, "assert_schema_current", Mock())
    if hasattr(entry, "_remove_pidfile"):
        monkeypatch.setattr(entry, "_remove_pidfile", Mock())
    return capture, create, pipeline, logging, exit_process


def test_entry_retains_one_gate_for_work_and_lazy_log_database(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = LoadedCommit(tmp_path, "captured-before-checkout-moved")
    capture, create, pipeline, logging, exit_process = _prepare_entry(entry, monkeypatch, image)
    count = Mock(return_value=7)
    received: list[Callable[[], Database]] = []

    async def work(
        *,
        process_config: ConfigBoot,
        runtime: Path,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> None:
        assert runtime == pipeline.call_args.kwargs["home"] / "run"
        received.append(database)
        assert image is capture.return_value
        database()
        previous_max = process_config.view.data_plane.db_pool_max_size
        process_config.view.data_plane.db_pool_max_size = previous_max + 1
        database()
        assert create.call_args.args[0].db_url == process_config.view.data_plane.db_url
        assert create.call_args.args[0].db_pool_max_size == previous_max + 1

    monkeypatch.setattr(entry, "run", work)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    entry.main()

    capture.assert_called_once_with()
    count.assert_not_called()
    factory = received[0]
    assert pipeline.call_args.kwargs["database"] is factory
    assert logging.call_args.kwargs["image"] is image
    assert logging.call_args.kwargs["producer"]() is pipeline.return_value
    assert (
        logging.call_args.kwargs["machine_reader"]() == pipeline.call_args.kwargs["machine"].name()
    )
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert len(gates) == 3 and all(gate is gates[0] for gate in gates)
    assert gates[0].application_name() == f"meiva:{logging.call_args.kwargs['name']}:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    gates[0].observe_minimum(7)
    assert not gates[0].min_read_due()
    exit_process.assert_called_once_with(0)


def test_work_failure_keeps_the_original_exception_and_exit_status(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture, _create, _pipeline, _logging, exit_process = _prepare_entry(
        entry, monkeypatch, LoadedCommit(Path(), None)
    )
    failure = RuntimeError("original service failure")

    async def work(
        *,
        process_config: ConfigBoot,
        runtime: Path,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> None:
        raise failure

    monkeypatch.setattr(entry, "run", work)
    entry.main()

    exit_process.assert_called_once_with(1)
    records = [record for record in caplog.records if record.exc_info]
    assert len(records) == 1
    assert records[0].exc_info is not None and records[0].exc_info[1] is failure
