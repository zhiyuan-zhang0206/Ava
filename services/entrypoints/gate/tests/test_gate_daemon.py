"""Gate entry owns one image, a lazy database gate and its writer's exit."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from base.cluster.machine import machine_name
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus
from services.entrypoints.gate import daemon


@dataclass
class _Entry:
    image: LoadedCommit
    calls: list[str] = field(default_factory=list[str])
    database: Callable[[], Any] | None = None
    logging: Mock = field(default_factory=Mock)
    server: Mock = field(default_factory=Mock)
    pipeline: Mock = field(default_factory=Mock)
    capture: Mock = field(default_factory=Mock)
    failures: dict[str, BaseException] = field(default_factory=dict[str, BaseException])

    def step(self, name: str) -> None:
        self.calls.append(name)
        if failure := self.failures.get(name):
            raise failure


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Entry:
    setup = _Entry(LoadedCommit(tmp_path, "captured-image"))
    setup.capture.return_value = setup.image
    monkeypatch.setattr(LoadedCommit, "capture", setup.capture)
    monkeypatch.setattr(sys, "argv", ["gate", "--port", "32001"])

    def build_pipeline(*, database: Callable[[], Any]) -> Any:
        setup.database = database
        setup.step("build")
        return setup.pipeline

    def logging(*args: Any, **kwargs: Any) -> None:
        setup.logging(*args, **kwargs)
        setup.step("logging")

    original_gate = daemon.Gate

    def gate(**kwargs: Any) -> daemon.Gate:
        setup.step("gate")
        return original_gate(**kwargs)

    def server(*args: Any, **kwargs: Any) -> Mock:
        setup.step("server")
        return setup.server

    setup.server.serve_forever.side_effect = lambda: setup.step("serve")
    setup.server.server_close.side_effect = lambda: setup.step("http-close")

    def stop(*, timeout: float) -> DrainResult:
        assert timeout == 2
        setup.step("pipeline-stop")
        return DrainResult(DrainStatus.COMPLETED, DrainPhase.STOP)

    def install_shutdown(name: str) -> None:
        assert name == "gate"

    setup.pipeline.stop.side_effect = stop
    monkeypatch.setattr("base.telemetry.emitter.build_pipeline", build_pipeline)
    monkeypatch.setattr("base.log.init_gateway_process", logging)
    monkeypatch.setattr("base.daemon.shutdown.install_graceful_shutdown", install_shutdown)
    monkeypatch.setattr(daemon, "Gate", gate)
    monkeypatch.setattr(daemon, "ThreadingHTTPServer", server)
    return setup


def test_entry_keeps_one_image_gate_factory_and_producer(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = Mock(return_value=17)
    monkeypatch.setattr("base.native_process.code_version.first_parent_count", version)
    opened: list[ProcessDbGate] = []

    def database(*, gate: ProcessDbGate) -> object:
        opened.append(gate)
        return object()

    monkeypatch.setattr(Database, "from_settings", database)
    daemon.main()
    entry.capture.assert_called_once_with()
    assert opened == []  # Neither pipeline construction nor boot eagerly dials DB.
    assert entry.database is not None
    assert entry.database() is not entry.database()
    assert len(opened) == 2 and opened[0] is opened[1]
    assert opened[0].min_read_due()  # Gate is not a CLI-exempt process.
    assert opened[0].application_name() == "ava:gate:v17"
    version.assert_called_once_with(entry.image.source_root, entry.image.sha)
    args, kwargs = entry.logging.call_args
    assert args == ("gate",)
    assert kwargs["image"] is entry.image
    assert kwargs["machine_reader"] is machine_name
    assert kwargs["producer"]() is entry.pipeline
    entry.pipeline.stop.assert_called_once_with(timeout=2)
    assert entry.calls == [
        "build",
        "logging",
        "gate",
        "server",
        "serve",
        "http-close",
        "pipeline-stop",
    ]


@pytest.mark.parametrize("stage", ["logging", "gate", "server", "serve"])
def test_startup_and_serve_failures_stop_the_writer_without_replacing_error(
    entry: _Entry, stage: str
) -> None:
    primary = RuntimeError(stage)
    entry.failures[stage] = primary
    with pytest.raises(RuntimeError) as caught:
        daemon.main()
    assert caught.value is primary
    entry.pipeline.stop.assert_called_once_with(timeout=2)
    assert entry.calls[-1] == "pipeline-stop"
    assert ("http-close" in entry.calls) is (stage == "serve")


def test_http_and_pipeline_cleanup_faults_remain_notes_on_serve_failure(entry: _Entry) -> None:
    primary = SystemExit("serve failed")
    entry.failures.update(
        {
            "serve": primary,
            "http-close": OSError("close failed"),
            "pipeline-stop": RuntimeError("writer failed"),
        }
    )
    with pytest.raises(SystemExit) as caught:
        daemon.main()
    assert caught.value is primary
    assert any("close failed" in note for note in primary.__notes__)
    assert any("writer failed" in note for note in primary.__notes__)
    assert entry.calls[-2:] == ["http-close", "pipeline-stop"]


@pytest.mark.parametrize("stage", ["http-close", "pipeline-stop"])
def test_cleanup_fault_without_primary_is_raised(entry: _Entry, stage: str) -> None:
    failure = RuntimeError(stage)
    entry.failures[stage] = failure
    with pytest.raises(RuntimeError) as caught:
        daemon.main()
    assert caught.value is failure
    entry.pipeline.stop.assert_called_once_with(timeout=2)


def test_interrupt_closes_http_before_stopping_writer(entry: _Entry) -> None:
    entry.failures["serve"] = KeyboardInterrupt()
    daemon.main()
    assert entry.calls[-2:] == ["http-close", "pipeline-stop"]


def test_unfinished_stop_is_reported_without_reentering_pipeline(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    drain = DrainResult(DrainStatus.UNFINISHED, DrainPhase.STOP)
    entry.pipeline.stop.side_effect = None
    entry.pipeline.stop.return_value = drain
    report = Mock()
    monkeypatch.setattr("base.telemetry.emitter.report_no_pipeline", report)
    daemon.main()
    entry.pipeline.stop.assert_called_once_with(timeout=2)
    assert "unfinished" in report.call_args.args[0]
    assert report.call_args.kwargs["drain"] is drain


def test_real_owned_pipeline_is_joined_on_http_exit(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = EventPipeline(writer=lambda _events: None)

    def build_pipeline(*, database: Callable[[], Any]) -> EventPipeline:
        del database
        return pipeline

    monkeypatch.setattr("base.telemetry.emitter.build_pipeline", build_pipeline)
    try:
        daemon.main()
        assert entry.logging.call_args.kwargs["producer"]() is pipeline
        assert pipeline.stopped
    finally:
        pipeline.stop(timeout=2)


def test_invalid_arguments_do_not_capture_or_construct_resources(entry: _Entry) -> None:
    sys.argv.append("--unknown-option")
    with pytest.raises(SystemExit) as caught:
        daemon.main()
    assert caught.value.code == 2
    entry.capture.assert_not_called()
    assert entry.calls == []
