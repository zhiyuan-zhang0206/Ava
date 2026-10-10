"""Heartbeat and IM entries share their captured image and gate with the log producer."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from base.db import Database
from base.deploy.schema import migrations
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit

_ENTRIES = (
    "services.wake.heartbeat.daemon",
    "services.entrypoints.im_bridge.daemon",
)


class _Runner:
    def run(self, work: Coroutine[Any, Any, None]) -> None:
        asyncio.run(work)


@pytest.fixture(params=_ENTRIES)
def entry(request: pytest.FixtureRequest) -> ModuleType:
    return importlib.import_module(request.param)


def _prepare_entry(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, image: LoadedCommit
) -> tuple[Mock, Mock, Mock, Mock, Mock]:
    capture = Mock(return_value=image)
    create = Mock(return_value=Mock(spec=Database))
    pipeline = Mock(return_value=object())
    logging = Mock()
    exit_process = Mock()
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(entry.Database, "from_settings", create)
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

    async def work(*, database: Callable[[], Database], image: LoadedCommit) -> None:
        received.append(database)
        assert image is capture.return_value
        database()
        database()

    monkeypatch.setattr(entry, "run", work)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    entry.main()

    capture.assert_called_once_with()
    count.assert_not_called()
    factory = received[0]
    assert pipeline.call_args.kwargs["database"] is factory
    assert logging.call_args.kwargs["image"] is image
    assert logging.call_args.kwargs["producer"]() is pipeline.return_value
    assert logging.call_args.kwargs["machine_reader"]() == entry.settings.general.machine_name
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert len(gates) == 3 and all(gate is gates[0] for gate in gates)
    assert gates[0].application_name() == f"ava:{logging.call_args.kwargs['name']}:v7"
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

    async def work(*, database: Callable[[], Database], image: LoadedCommit) -> None:
        raise failure

    monkeypatch.setattr(entry, "run", work)
    entry.main()

    exit_process.assert_called_once_with(1)
    records = [record for record in caplog.records if record.exc_info]
    assert len(records) == 1
    assert records[0].exc_info is not None and records[0].exc_info[1] is failure
