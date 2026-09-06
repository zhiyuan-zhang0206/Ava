"""Expected-record maintenance delivery uses the existing private-console helper."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from shared import winproc
from shared.session_backend import WinprocSessionBackend
from shared.session_record import SessionRecord


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
    return value


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


def test_windows_terminal_filter_does_not_mistake_services_for_shells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _maintenance_stop as stop

    backend = WinprocSessionBackend()
    monkeypatch.setattr(stop, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(stop, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(backend, "list_sessions", lambda: ["ava-agent-host", "ava-watchdog"])
    stop.require_no_terminals()
    monkeypatch.setattr(
        backend, "list_sessions", lambda: ["ava-agent-host", "ava-agent-123-shell-4"]
    )
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        stop.require_no_terminals()
