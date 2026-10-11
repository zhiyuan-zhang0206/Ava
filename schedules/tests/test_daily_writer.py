"""Direct schedules own a writer; runner-hosted templates only borrow it."""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import ava
from base.cluster.machine import MachineIdentity
from base.telemetry import EventPipeline
from base.native_process.loaded_commit import LoadedCommit
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import fresh_schedule_entry, schedule_entry
from schedules import entry


def test_quiet_schedule_exit_never_builds_a_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("quiet schedule must not start a writer")

    monkeypatch.setattr("threading.Thread", forbidden)
    with patch.dict(os.environ), fresh_schedule_entry() as inputs:
        assert callable(inputs.producer)
        assert ava.context.require_machine() is inputs.machine


def test_schedule_closes_constructed_writer_and_preserves_loop_error() -> None:
    primary = RuntimeError("original schedule loop error")

    pipeline: EventPipeline | None = None
    prior = getattr(ava, "context", None)
    with (
        pytest.raises(RuntimeError) as caught,
        patch.dict(os.environ),
        fresh_schedule_entry() as inputs,
    ):
        pipeline = inputs.producer()
        assert inputs.producer() is pipeline
        assert ava.context.require_machine() is inputs.machine
        raise primary
    assert caught.value is primary
    assert pipeline is not None and pipeline.stopped
    assert getattr(ava, "context", None) is prior


def test_runner_inputs_are_borrowed_without_rebuilding_or_closing(
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a template must not build a second entry owner")

    monkeypatch.setattr(ava, "loaded_code_image", forbidden)
    monkeypatch.setattr(entry, "ClientSet", forbidden)
    database = Mock()
    producer = Mock()
    inputs = ScheduleInputs(
        database, producer, LoadedCommit(Path("/entry"), None), machine=machine_identity
    )
    prior = getattr(ava, "context", None)
    primary = RuntimeError("borrowed loop failed")
    with pytest.raises(RuntimeError) as caught, schedule_entry(inputs) as borrowed:
        assert borrowed is inputs
        assert borrowed.database() is database.return_value
        assert getattr(ava, "context", None) is prior
        raise primary
    assert caught.value is primary
    producer.assert_not_called()
    database.return_value.close.assert_not_called()
