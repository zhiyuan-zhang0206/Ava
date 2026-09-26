"""Expected-record maintenance delivery uses the existing private-console helper."""

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import psutil
import pytest

from shared import paths, winproc
from shared.native_process.ownership import OwnedProcess
from shared.session_backend import WinprocSessionBackend
from shared.session_record import SessionRecord
from shared.windows_terminal import record as terminal_record
from shared.windows_terminal.backend import WindowsTerminalBackend


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unexpected helper call")


def helper_success(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stderr="")


@pytest.fixture
def record(monkeypatch: pytest.MonkeyPatch) -> SessionRecord:
    value = SessionRecord(
        123, 5.0, "private-fixture", "/private-test", 5.0, control_mode="private-console-v1"
    )

    def read(_name: str) -> SessionRecord:
        return value

    def process(_record: SessionRecord) -> SimpleNamespace:
        return SimpleNamespace(create_time=lambda: 5.0)

    monkeypatch.setattr(winproc, "_read_record", read)
    monkeypatch.setattr(winproc, "_process_for_record", process)
    # Same-session by default: these tests exercise the direct helper path.
    monkeypatch.setattr(winproc, "process_session_id", _session_of)
    monkeypatch.setattr(winproc, "current_session_id", _caller_session)
    return value


def _session_of(_pid: int) -> int:
    return 7


def _caller_session() -> int:
    return 7


def test_replacement_is_refused_before_helper(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert not winproc.graceful_signal("service", expected=replace(record, pid=124))


def test_record_change_during_capture_refuses(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = iter([record, replace(record, pid=124)])

    def read(_name: str) -> SessionRecord:
        return next(calls)

    monkeypatch.setattr(winproc, "_read_record", read)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert not winproc.graceful_signal("service", expected=record)


def test_helper_receives_remaining_budget_and_expected_identity(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc.time, "monotonic", lambda: 100.0)
    calls: list[tuple[list[str], float]] = []

    def helper(args: list[str], *, timeout: float) -> SimpleNamespace:
        calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(winproc, "run_job_process", helper)
    assert WinprocSessionBackend().graceful_signal("service", expected=record, timeout=0.2)
    args, timeout = calls[0]
    assert timeout == 0.2
    assert args[-3:] == ["123", "5.0", "100.2"]


def test_late_helper_success_is_timeout(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([100.0, 101.0])
    monkeypatch.setattr(winproc.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(winproc, "run_job_process", helper_success)
    with pytest.raises(TimeoutError):
        winproc.graceful_signal("service", expected=record, timeout=0.1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_never_calls_helper(
    record: SessionRecord, timeout: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(ValueError):
        winproc.graceful_signal("service", expected=record, timeout=timeout)


def _windows_terminal(name: str, cwd: Path) -> Path:
    """Publish one active root-brokered Windows terminal record (owner still pending)."""
    birth = terminal_record.NativeBirth.capture(OwnedProcess.capture(psutil.Process()))
    value = terminal_record.TerminalRecord(
        name=name,
        domain="a" * 32,
        generation=None,
        state="pending",
        root=birth,
        launcher=birth,
        started_at=time.time(),
        command="shell",
        cwd=str(cwd),
    )
    path = terminal_record.record_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.model_dump_json())
    return path


@pytest.fixture
def windows_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Maintenance stop on Windows: terminals are the root-brokered records."""
    from cli.commands import _maintenance_stop as stop

    monkeypatch.setattr(paths, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(stop, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(stop, "get_shell_backend", WindowsTerminalBackend)
    return tmp_path


def test_windows_terminal_scan_does_not_mistake_services_for_shells(
    windows_run_dir: Path,
) -> None:
    from cli.commands import _maintenance_stop as stop

    for index, name in enumerate(["ava-agent-host", "ava-schedule-indexer"]):
        SessionRecord(900000 + index, 1.0, "fixture", str(windows_run_dir), 1.0).write(
            windows_run_dir / "sessions" / f"{name}.json"
        )
    stop.require_no_terminals()
    _windows_terminal("ava-schedule-7", windows_run_dir)
    with pytest.raises(RuntimeError, match="will not kill or replay") as refused:
        stop.require_no_terminals()
    assert "['ava-schedule-7']" in str(refused.value)


def test_keep_windows_terminals_excludes_them_from_the_root_service_stop(
    windows_run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _maintenance_stop as stop
    from cli.commands import _root_driver

    terminals = {
        name: _windows_terminal(name, windows_run_dir)
        for name in ["ava-agent-123-shell-4", "ava-schedule-7", "ava-agent-123-shell-5-old"]
    }
    before = {name: path.read_bytes() for name, path in terminals.items()}
    services = {"ava-agent-host": "agent-host", "ava-schedule-indexer": "schedule-indexer"}
    calls: list[dict[str, object]] = []

    def selection() -> dict[str, str]:
        return dict(services)

    def stop_tree(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(_root_driver, "_root_tree_selection", selection)
    monkeypatch.setattr(_root_driver, "_stop_root_service_tree", stop_tree)
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        stop.stop_services(1)
    assert not calls
    assert stop.stop_services(1, keep_terminals=True) == sorted(services)
    assert len(calls) == 1
    assert calls[0]["preserve"] == frozenset()
    assert calls[0]["selected"] is None
    assert calls[0]["force"] is False
    assert WindowsTerminalBackend().list_sessions() == sorted(terminals)
    assert {name: path.read_bytes() for name, path in terminals.items()} == before


def test_drifted_create_time_is_not_a_replacement(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivery re-check carries the record-resolution tolerance.

    Mirror of posixproc: a create_time reading that moved by whole seconds for
    the same live process must not refuse delivery.
    """

    def drifted_process(_record: SessionRecord) -> SimpleNamespace:
        return SimpleNamespace(create_time=lambda: 6.0)

    monkeypatch.setattr(winproc, "_process_for_record", drifted_process)
    monkeypatch.setattr(winproc.time, "monotonic", lambda: 100.0)
    calls: list[tuple[list[str], float]] = []

    def helper(args: list[str], *, timeout: float) -> SimpleNamespace:
        calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(winproc, "run_job_process", helper)

    assert winproc.graceful_signal("service", expected=record, timeout=0.2)
    # The delivered identity stays the record's birth, not the drifted reading.
    assert calls[0][0][-3:] == ["123", "5.0", "100.2"]


def test_birth_beyond_tolerance_still_refuses_before_helper(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replaced_process(_record: SessionRecord) -> SimpleNamespace:
        return SimpleNamespace(create_time=lambda: 65.0)

    monkeypatch.setattr(winproc, "_process_for_record", replaced_process)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert not winproc.graceful_signal("service", expected=record)
