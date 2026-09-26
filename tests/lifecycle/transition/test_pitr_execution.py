"""PITR phase interruption never selects another image or repeats SQL drain offline."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.release_transition import execute, journal, pitr
from cli.release_transition.pitr_evidence import DataOwner, DataStop
from cli.release_transition.request import PitrRequest
from shared.process_evidence import ExpectedProcess
from tests.lifecycle.transition.test_launcher_linux import planned as planned
from tests.lifecycle.transition.test_pitr_operation import _seal
from tests.lifecycle.transition.test_pitr_operation import pitr_request as pitr_request


def _constant[T](value: T) -> Callable[..., T]:
    def fixed(*_args: object, **_kwargs: object) -> T:
        return value

    return fixed


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize(
    "crash", ["stopping_apps", "stopping_data", "starting", "observing", "resuming", "proving"]
)
def test_interruption_replays_only_pending_pitr_phase(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    events: list[str] = []
    failed = False

    class Effects:
        def __init__(self, _request: PitrRequest) -> None:
            pass

        def _effect(self, phase: str) -> None:
            nonlocal failed
            operation = journal.read_operation(pitr_request.path)
            assert operation.phase == phase
            assert operation.reference == pitr_request.image
            events.append(phase)
            if phase == crash and not failed:
                failed = True
                raise Interrupted

        def preflight(self) -> None:
            self._effect("prepared")

        def provision(self, handle: journal.Journal) -> bool:
            self._effect("provisioning")
            handle.provisioned(_seal(pitr_request, "f" * 64))
            return True

        def quiesce(self, _operation: journal.Operation) -> None:
            self._effect("quiescing")

        def stop_apps(self, _journal: journal.Journal) -> None:
            self._effect("stopping_apps")

        def stop_data(self, _operation: journal.Operation) -> None:
            self._effect("stopping_data")

        def start(self, _operation: journal.Operation) -> None:
            self._effect("starting")

        def observe(self, _operation: journal.Operation) -> None:
            self._effect("observing")

        def resume(self, _operation: journal.Operation) -> None:
            self._effect("resuming")

        def prove(self, _journal: journal.Journal) -> None:
            self._effect("proving")

    def new_interpreter(operation: journal.Operation) -> None:
        assert operation.phase == "quiescing"
        assert operation.pitr is not None and operation.pitr.seal is not None
        raise Interrupted

    monkeypatch.setattr(pitr, "PitrTransition", Effects)
    monkeypatch.setattr(execute, "reenter", new_interpreter)
    journal.create(pitr_request)
    with journal.exclusive(pitr_request.path) as handle, pytest.raises(Interrupted):
        execute.drive_pitr(handle)
    assert journal.read_operation(pitr_request.path).phase == "quiescing"
    with journal.exclusive(pitr_request.path) as handle, pytest.raises(Interrupted):
        execute.drive_pitr(handle)
    assert journal.read_operation(pitr_request.path).phase == crash
    with journal.exclusive(pitr_request.path) as handle:
        execute.drive_pitr(handle)
        assert handle.operation.terminal
    assert events.count("prepared") == events.count("provisioning") == 1
    assert events.count(crash) == 2
    assert events.count("quiescing") == 1
    if crash != "stopping_apps":
        assert events.count("stopping_apps") == 1


def _custody(home: Path) -> DataStop:
    def owner(pid: int, name: str, port: int) -> DataOwner:
        native = ExpectedProcess(pid=pid, create_time=2.0, starttime=pid)
        return DataOwner(process=native, tree=(native,), directory=str(home / name), port=port)

    return DataStop(
        postgres=owner(41, "pg", 18000), redis=owner(42, "redis", 18001), pgbouncer=None
    )


def test_db_down_stop_continuation_uses_persisted_native_receipt_without_sql_drain(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _maintenance_data_plane, _root_driver
    from shared import maintenance

    receipt = _custody(Path(pitr_request.home))
    operation = journal.create(pitr_request)
    assert operation.pitr is not None
    progress = operation.pitr.model_copy(
        update={"data_stop": receipt, "seal": _seal(pitr_request, "f" * 64)}
    )
    operation = operation.model_copy(update={"phase": "stopping_data", "pitr": progress})
    driver = object.__new__(pitr.PitrTransition)
    driver.request, driver.home = pitr_request, Path(pitr_request.home)
    observed: list[object] = []
    monkeypatch.setattr(pitr, "require_inputs", _constant(None))
    monkeypatch.setattr(
        maintenance,
        "require_operation",
        _constant(SimpleNamespace(maintenance=SimpleNamespace(phase="stopped"))),
    )
    monkeypatch.setattr(
        _root_driver, "_require_root_absent", lambda: observed.append("root absent")
    )

    def close_custody(value: DataStop, _timeout: float) -> None:
        observed.append(value)

    monkeypatch.setattr(_maintenance_data_plane, "stop_captured", close_custody)

    def no_database(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline continuation attempted database access")

    monkeypatch.setattr("shared.db.connect", no_database)
    monkeypatch.setattr("shared.db.pool", no_database)
    monkeypatch.setattr("ops.agent_pause._drain", no_database)
    driver.stop_data(operation)
    assert observed == ["root absent", receipt]


def test_failed_pitr_start_keeps_action_without_automatic_release_recovery(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = journal.create(pitr_request)
    assert operation.pitr is not None
    with journal.exclusive(pitr_request.path) as handle:
        handle.advance("provisioning")
        handle.provisioned(_seal(pitr_request, "f" * 64))
        for phase in ("stopping_apps", "stopping_data", "starting"):
            handle.advance(phase)

    class Failing:
        def __init__(self, _request: PitrRequest) -> None:
            pass

        def start(self, _operation: journal.Operation) -> None:
            raise RuntimeError("native start failed")

    monkeypatch.setattr(pitr, "PitrTransition", Failing)
    with (
        journal.exclusive(pitr_request.path) as handle,
        pytest.raises(RuntimeError, match="native start failed"),
    ):
        execute.drive_pitr(handle)
    failed = journal.read_operation(pitr_request.path)
    assert failed.phase == "starting" and failed.direction is None
    assert failed.pitr is not None and failed.pitr.action == "activate"
    assert failed.error == "RuntimeError: native start failed"


def test_preparation_lease_failure_cannot_mutate_business_state(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr.activation_state import load_record
    from shared import cluster_lock
    from shared.release_operation import authorized_pitr

    journal.create(pitr_request)
    driver = object.__new__(pitr.PitrTransition)
    driver.request, driver.home = pitr_request, Path(pitr_request.home)
    monkeypatch.setattr(cluster_lock, "acquire_update_lock", _constant(False))
    with journal.exclusive(pitr_request.path) as handle:
        handle.advance("provisioning")
        with (
            authorized_pitr(pitr_request.path, handle.pitr_record_write),
            pytest.raises(RuntimeError, match="online writer"),
        ):
            driver.provision(handle)
        record = load_record(driver.home)
        assert record is not None and record.phase == "shadow"
        assert record.pre_activation_snapshot is None
        assert handle.operation.phase == "provisioning"
    assert (driver.home / "pg/postgresql.auto.conf").read_bytes() == b"# owned\n"


def test_sealed_reentry_executes_only_retained_argv_and_environment(
    planned: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import psutil

    from cli.release_transition import launcher_linux
    from shared.native_process.ownership import OwnedProcess

    operation = journal.read_operation(Path(planned["operation"]))
    identity = OwnedProcess.capture(psutil.Process())
    monkeypatch.setattr(launcher_linux, "readback", _constant(SimpleNamespace(owner=identity)))
    events: list[object] = []
    monkeypatch.setenv("PATH", "/untrusted/caller")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/imports")
    monkeypatch.setattr(os, "chdir", events.append)

    def entered(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        events.append((executable, argv, environment))
        raise Interrupted

    monkeypatch.setattr(os, "execve", entered)
    with pytest.raises(Interrupted):
        execute.reenter(operation)
    assert events == [
        planned["cwd"],
        (planned["interpreter"], planned["argv"], planned["environment"]),
    ]
    assert planned["argv"][1:3] == ["-I", "-B"]
    assert planned["argv"][-4:] == [
        "-m",
        "cli.release_transition.execute",
        "--operation",
        planned["operation"],
    ]
    monkeypatch.setattr(launcher_linux, "readback", _constant(SimpleNamespace(owner=None)))
    events.clear()
    with pytest.raises(RuntimeError, match="same native executor"):
        execute.reenter(operation)
    assert events == []


def test_failed_online_lease_keeps_business_diagnostics_and_operation_authority(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr.activation_state import load_record
    from shared import cluster_lock
    from shared.release_operation import authorized_pitr

    journal.create(pitr_request)
    driver = object.__new__(pitr.PitrTransition)
    driver.request, driver.home = pitr_request, Path(pitr_request.home)
    monkeypatch.setattr(pitr, "PitrTransition", _constant(driver))
    monkeypatch.setattr(cluster_lock, "acquire_update_lock", _constant(False))
    with journal.exclusive(pitr_request.path) as handle:
        handle.advance("provisioning")
        with (
            authorized_pitr(pitr_request.path, handle.pitr_record_write),
            pytest.raises(RuntimeError, match="online writer"),
        ):
            execute.drive_pitr(handle)
        record = load_record(driver.home)
        assert record is not None and record.phase == "shadow" and record.error == "RuntimeError"
        assert handle.operation.phase == "provisioning" and handle.operation.error is not None
        assert handle.operation.pitr is not None and handle.operation.pitr.record_intent is not None


def test_data_capture_refuses_postgres_replacement_before_any_data_signal(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _maintenance, _maintenance_data_plane, _root_driver
    from shared import maintenance

    journal.create(pitr_request)
    driver = object.__new__(pitr.PitrTransition)
    driver.request, driver.home = pitr_request, Path(pitr_request.home)
    receipt = _custody(driver.home)
    newer = ExpectedProcess(pid=99, create_time=3.0, starttime=9)
    receipt = receipt.model_copy(
        update={
            "postgres": receipt.postgres.model_copy(update={"process": newer, "tree": (newer,)})
        }
    )
    monkeypatch.setattr(pitr, "require_inputs", _constant(None))
    monkeypatch.setattr(
        maintenance,
        "require_operation",
        _constant(SimpleNamespace(maintenance=SimpleNamespace(phase="drained"))),
    )
    monkeypatch.setattr(_maintenance, "_stop", _constant(None))
    monkeypatch.setattr(_root_driver, "_require_root_absent", _constant(None))
    monkeypatch.setattr(_maintenance_data_plane, "capture_custody", _constant(receipt))
    with journal.exclusive(pitr_request.path) as handle:
        handle.advance("provisioning")
        handle.provisioned(_seal(pitr_request, "f" * 64))
        handle.advance("stopping_apps")
        with pytest.raises(RuntimeError, match="replaced before"):
            driver.stop_apps(handle)
        assert handle.operation.pitr is not None and handle.operation.pitr.data_stop is None
