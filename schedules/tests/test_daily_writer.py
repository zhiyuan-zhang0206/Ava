"""Direct schedules own a writer; runner-hosted templates only borrow it."""

from pathlib import Path
from unittest.mock import Mock

import pytest

import ava
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.telemetry import EventPipeline
from base.native_process.loaded_commit import LoadedCommit
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import schedule_entry
from schedules import entry


def test_quiet_schedule_exit_never_builds_a_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("quiet schedule must not start a writer")

    monkeypatch.setattr("threading.Thread", forbidden)
    with schedule_entry(None) as inputs:
        assert callable(inputs.producer)


def test_schedule_closes_constructed_writer_and_preserves_loop_error(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("original schedule loop error")

    def make_database(*, gate: ProcessDbGate) -> Database:
        return database

    pipeline: EventPipeline | None = None
    monkeypatch.setattr(entry.Database, "from_settings", make_database)
    with pytest.raises(RuntimeError) as caught, schedule_entry(None) as inputs:
        pipeline = inputs.producer()
        assert inputs.producer() is pipeline
        raise primary
    assert caught.value is primary
    assert pipeline is not None and pipeline.stopped


def test_runner_inputs_are_borrowed_without_rebuilding_or_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a template must not build a second entry owner")

    monkeypatch.setattr(ava, "loaded_code_image", forbidden)
    monkeypatch.setattr(entry, "ClientSet", forbidden)
    database = Mock()
    producer = Mock()
    inputs = ScheduleInputs(database, producer, LoadedCommit(Path("/entry"), None))
    primary = RuntimeError("borrowed loop failed")
    with pytest.raises(RuntimeError) as caught, schedule_entry(inputs) as borrowed:
        assert borrowed is inputs
        assert borrowed.database() is database.return_value
        raise primary
    assert caught.value is primary
    producer.assert_not_called()
    database.return_value.close.assert_not_called()
