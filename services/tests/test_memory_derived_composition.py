"""Memory service entries own one captured gate and a bounded pipeline lifetime."""

from __future__ import annotations

import asyncio
import importlib
import os
import time
from collections.abc import Coroutine, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from base.db import Database
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus

_ENTRIES = ("services.derived.memory_search.daemon", "services.derived.memory_indexer.daemon")


@pytest.fixture(autouse=True)
def _owned_config_environment() -> Iterator[None]:
    try:
        with patch.dict(os.environ):
            yield
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


class _Runner:
    def run(self, work: Coroutine[Any, Any, None]) -> None:
        asyncio.run(work)


@pytest.fixture(params=_ENTRIES)
def entry(request: pytest.FixtureRequest) -> ModuleType:
    return importlib.import_module(request.param)


def _prepare(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Mock, Mock, Mock, Mock, Mock, list[str]]:
    capture = Mock(return_value=LoadedCommit(tmp_path, "captured-memory-entry"))
    create = Mock(side_effect=Database)
    events: list[str] = []
    pipeline = Mock()

    def stop(*, timeout: float) -> DrainResult:
        assert timeout == 2
        events.append("pipeline_stopped")
        return DrainResult(DrainStatus.COMPLETED, DrainPhase.STOP)

    pipeline.stop.side_effect = stop
    build = Mock(return_value=pipeline)
    logging = Mock()

    def exit_entry(code: int) -> None:
        events.append("hard_exit")

    exit_process = Mock(side_effect=exit_entry)
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(entry, "Database", create)
    monkeypatch.setattr(entry, "build_pipeline", build)
    monkeypatch.setattr(entry, "init_gateway_process", logging)
    monkeypatch.setattr(entry, "install_graceful_shutdown", Mock())
    monkeypatch.setattr(entry, "asyncio", SimpleNamespace(Runner=_Runner))
    monkeypatch.setattr(entry, "_hard_exit", exit_process)
    monkeypatch.setattr(entry, "_is_running", Mock(return_value=False))
    monkeypatch.setattr(entry, "acquire_pidfile", Mock(return_value=True))
    monkeypatch.setattr(entry, "remove_pidfile", Mock())
    return capture, create, build, logging, exit_process, events


def test_entry_shares_gate_and_closes_pipeline_before_hard_exit(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture, create, build, logging, exit_process, events = _prepare(entry, monkeypatch, tmp_path)
    count = Mock(return_value=7)
    work_inputs: list[dict[str, Any]] = []

    async def run(*_args: Any, **kwargs: Any) -> None:
        work_inputs.append(kwargs)
        if "database" in kwargs:
            assert kwargs["image"] is capture.return_value
            kwargs["database"]()

    monkeypatch.setattr(entry, "run", run)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    entry.main()
    capture.assert_called_once_with()
    count.assert_not_called()
    factory = build.call_args.kwargs["database"]
    factory()
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert all(gate is gates[0] for gate in gates)
    assert gates[0].application_name() == f"ava:{logging.call_args.kwargs['name']}:v7"
    assert logging.call_args.kwargs["image"] is capture.return_value
    assert logging.call_args.kwargs["producer"]() is build.return_value
    if "database" in work_inputs[0]:
        assert work_inputs[0]["database"] is factory
    else:
        assert len(gates) == 2  # The search server itself never constructed a database.
    build.return_value.stop.assert_called_once_with(timeout=2)
    assert events == ["pipeline_stopped", "hard_exit"]
    exit_process.assert_called_once_with(0)


@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_logging_start_failure_preserves_primary_and_stops_pipeline(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_fault: bool
) -> None:
    _capture, _create, build, logging, exit_process, events = _prepare(entry, monkeypatch, tmp_path)
    failure = RuntimeError("original logging startup failure")
    logging.side_effect = failure
    if cleanup_fault:
        build.return_value.stop.side_effect = RuntimeError("secondary cleanup failure")
    with pytest.raises(RuntimeError) as raised:
        entry.main()
    assert raised.value is failure
    build.return_value.stop.assert_called_once_with(timeout=2)
    assert events == ([] if cleanup_fault else ["pipeline_stopped"])
    exit_process.assert_not_called()


def test_failed_pipeline_stop_keeps_failed_exit(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _capture, _create, build, _logging, exit_process, _events = _prepare(
        entry, monkeypatch, tmp_path
    )

    async def run(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(entry, "run", run)
    build.return_value.stop.side_effect = RuntimeError("pipeline cleanup failure")
    entry.main()
    build.return_value.stop.assert_called_once_with(timeout=2)
    exit_process.assert_called_once_with(1)


def test_unfinished_stop_reports_receipt_and_keeps_the_bounded_exit(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _capture, _create, build, _logging, exit_process, _events = _prepare(
        entry, monkeypatch, tmp_path
    )

    async def run(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(entry, "run", run)
    build.return_value.stop.side_effect = None
    build.return_value.stop.return_value = DrainResult(DrainStatus.UNFINISHED, DrainPhase.STOP)
    entry.main()
    build.return_value.stop.assert_called_once_with(timeout=2)
    assert "event pipeline stop unfinished" in caplog.text
    exit_process.assert_called_once_with(0)
