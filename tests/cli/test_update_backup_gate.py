"""The gateway update stop refuses while the backup pipeline is in flight.

Task #3661: the 2026-09-16 wave abort (rc=5) was the local stop meeting the
daily dump's off-site publish. The pre-check replays the operator's manual
gate: the scheduler's `/healthz` progress field and a stand-alone publish
process, refusal before anything is stopped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.commands import _update_backup_gate as gate
from cli.commands import _update_local, _update_recover
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE, STOP_INCOMPLETE_EXIT_CODE


class _Proc:
    """One `psutil.process_iter` row, as the prefilled `.info` presents it."""

    def __init__(self, pid: int, cmdline: list[str], status: str = "running") -> None:
        self.info: dict[str, object] = {"pid": pid, "cmdline": cmdline, "status": status}


def _health(progress: str | None, *, component: str = "backup") -> dict[str, object]:
    record: dict[str, object] = {"name": component, "status": "ok"}
    if progress is not None:
        record["progress"] = progress
    return {"name": "pg_backup", "components": [record]}


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate.psutil, "process_iter", lambda _attrs: iter(()))  # pyright: ignore[reportUnknownArgumentType]


def _publish_proc(pid: int, artifact: Path, *, status: str = "running") -> _Proc:
    return _Proc(
        pid,
        ["/x/.venv/bin/python", "-m", "services.backup", "--publish-offsite", str(artifact)],
        status,
    )


def test_running_scheduler_job_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gate,
        "read_health_payload",
        lambda _name, *, timeout_s: _health("running 1316s"),  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    )
    _no_processes(monkeypatch)
    assert gate.refuse_inflight_backup() == RESTART_DECLINED_EXIT_CODE
    err = capsys.readouterr().err
    assert "refusing the update stop" in err
    assert "progress='running 1316s'" in err
    assert "healthz component 'backup'" in err


def test_standalone_publish_process_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = gate.backup_dir() / "ava_main-20260915T190009Z.dump.enc"
    monkeypatch.setattr(gate, "read_health_payload", lambda _name, *, timeout_s: _health("idle"))  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        gate.psutil,
        "process_iter",
        lambda _attrs: iter((_publish_proc(4242, artifact),)),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert gate.refuse_inflight_backup() == RESTART_DECLINED_EXIT_CODE
    err = capsys.readouterr().err
    assert "ava_main-20260915T190009Z.dump.enc" in err
    assert "pid 4242" in err


def test_idle_scheduler_and_no_publish_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "read_health_payload", lambda _name, *, timeout_s: _health("idle"))  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    _no_processes(monkeypatch)
    assert gate.refuse_inflight_backup() is None
    captured = capsys.readouterr()
    assert "backup precheck: idle" in captured.out
    assert captured.err == ""


def test_unreadable_scheduler_is_reported_but_not_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "read_health_payload", lambda _name, *, timeout_s: None)  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    _no_processes(monkeypatch)
    assert gate.refuse_inflight_backup() is None
    out = capsys.readouterr().out
    assert "no in-flight pipeline found" in out
    assert "did not answer" in out


def test_missing_progress_field_is_reported_but_not_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "read_health_payload", lambda _name, *, timeout_s: _health(None))  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    _no_processes(monkeypatch)
    assert gate.refuse_inflight_backup() is None
    assert "no backup progress" in capsys.readouterr().out


def test_zombie_and_foreign_unit_publishes_are_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "read_health_payload", lambda _name, *, timeout_s: _health("idle"))  # noqa: ARG005 — signature parity with read_health_payload  # pyright: ignore[reportUnknownArgumentType]
    zombie = _publish_proc(1, gate.backup_dir() / "old.dump.enc", status="zombie")
    foreign = _publish_proc(2, Path("/other-unit/backups/db/other.dump.enc"))
    monkeypatch.setattr(gate.psutil, "process_iter", lambda _attrs: iter((zombie, foreign)))  # pyright: ignore[reportUnknownArgumentType]
    assert gate.refuse_inflight_backup() is None
    assert "backup precheck: idle" in capsys.readouterr().out


def test_disabled_precheck_passes_despite_a_running_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "_precheck_enabled", lambda: False)
    _no_processes(monkeypatch)
    assert gate.refuse_inflight_backup() is None
    assert "disabled" in capsys.readouterr().out


def test_gateway_local_update_refuses_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = MagicMock()
    monkeypatch.setattr("cli.commands.update._do_stop", stop)
    monkeypatch.setattr(gate, "refuse_inflight_backup", lambda: RESTART_DECLINED_EXIT_CODE)
    rc = _update_local._run_gateway_local_update(Path(), pull=False, origin="test")
    assert rc == RESTART_DECLINED_EXIT_CODE
    stop.assert_not_called()


def test_gateway_local_update_stops_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = MagicMock(return_value=1)
    monkeypatch.setattr("cli.commands.update._do_stop", stop)
    monkeypatch.setattr(gate, "refuse_inflight_backup", lambda: None)
    rc = _update_local._run_gateway_local_update(Path(), pull=False, origin="test")
    assert rc == STOP_INCOMPLETE_EXIT_CODE
    stop.assert_called_once()


def test_local_update_failure_detail_names_the_refusal() -> None:
    detail = _update_recover.local_update_failure_detail(
        RESTART_DECLINED_EXIT_CODE, restart_only=True
    )
    assert "refused before stopping" in detail
    assert "backup" in detail
    assert "retry once it is idle" in detail
