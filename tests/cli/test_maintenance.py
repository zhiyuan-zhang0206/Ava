"""Local CLI phases must retain the hold across failures and explicit startup."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from cli.commands import _maintenance as command
from cli.commands._maintenance_probe import HostIdentity
from shared import maintenance, pause_owner
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.fixture(autouse=True)
def cli_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(command, "host_identity", lambda: HostIdentity(uuid4(), frozenset()))
    monkeypatch.setattr(command, "connect", MagicMock())
    monkeypatch.setattr(command.maintenance_cohort, "verify_drained", MagicMock())
    monkeypatch.setattr(command, "_wake", MagicMock())
    monkeypatch.setattr("shared.host_deploy_state.set_posture", MagicMock())
    monkeypatch.setattr(command, "ops_quiescent", MagicMock())


def phase(value: str) -> None:
    before = pause_owner.begin_maintenance("local", WHEN)
    assert before.maintenance is not None
    hold = MaintenanceHold.decode({**before.maintenance.encode(), "phase": value})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, hold)


def test_stop_failure_retains_generation_and_retry_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase("drained")

    def timeout(_timeout: float) -> list[str]:
        raise TimeoutError("owned process still alive")

    monkeypatch.setattr(command, "stop_services", timeout)
    with pytest.raises(TimeoutError, match="still alive"):
        command._stop("local", WHEN, 2, gateway_last=False)
    assert command._hold("local", WHEN).phase == "stopping"
    with pytest.raises(RuntimeError, match="cannot release"):
        maintenance.require_start_allowed()
    monkeypatch.setattr(command, "stop_services", MagicMock(return_value=[]))
    command._stop("local", WHEN, 2, gateway_last=False)
    assert command._hold("local", WHEN).phase == "stopped"


def test_gateway_last_is_required_before_any_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("drained")
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"gateway"}))
    stop = MagicMock()
    monkeypatch.setattr(command, "stop_services", stop)
    with pytest.raises(RuntimeError, match="gateway-last"):
        command._stop("local", WHEN, 2, gateway_last=False)
    stop.assert_not_called()
    assert command._hold("local", WHEN).phase == "drained"


def test_start_keeps_hold_until_explicit_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("stopped")

    def start(**kwargs: Any) -> int:
        maintenance.require_start_allowed()
        assert maintenance.held()
        assert kwargs == {"persist_services": False}
        return 0

    def unpause() -> None:
        maintenance.require_start_allowed()
        assert maintenance.held()

    monkeypatch.setattr("cli.commands.start.cmd_start", start)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    assert command._start("local", WHEN) == 0
    assert command._hold("local", WHEN).phase == "ready"
    with pytest.raises(RuntimeError, match="cannot release"):
        maintenance.require_start_allowed()
    command._resume("local", WHEN, cancel=False)
    assert not maintenance.held()
    assert pause_owner.read().status == "resumed"


def test_failed_dependency_resume_never_releases_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("preparing")

    def unavailable() -> None:
        raise ConnectionError("dependencies unavailable")

    monkeypatch.setattr(command, "connect", unavailable)
    with pytest.raises(ConnectionError):
        command._resume("local", WHEN, cancel=True)
    assert command._hold("local", WHEN).phase == "preparing"


def test_failed_start_remains_retryable_under_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("stopped")
    monkeypatch.setattr("cli.commands.start.cmd_start", MagicMock(return_value=7))
    assert command._start("local", WHEN) == 7
    assert command._hold("local", WHEN).phase == "starting"

    with pytest.raises(RuntimeError, match="requires maintenance start"):
        command._resume("local", WHEN, cancel=False)


@pytest.mark.parametrize("value", ["stopping", "stopped", "starting", "ready"])
def test_cancel_cannot_bypass_stop_or_start_readiness(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    phase(value)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    with pytest.raises(RuntimeError, match="cancel cannot bypass"):
        command._resume("local", WHEN, cancel=True)
    unpause.assert_not_called()
    assert command._hold("local", WHEN).phase == value


@pytest.mark.parametrize("value", ["preparing", "draining", "drained"])
def test_cancel_can_abandon_drain_before_service_stop(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    phase(value)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    command._resume("local", WHEN, cancel=True)
    unpause.assert_called_once()
    assert not maintenance.held()


def test_real_parser_exposes_host_local_maintenance() -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    parsed = parser.parse_args(
        [
            "maintenance",
            "drain",
            "--operation",
            "local",
            "--acquired-at",
            WHEN.isoformat(),
            "--timeout",
            "3",
        ]
    )
    assert parsed.maintenance_cmd == "drain"
    assert parsed.operation == "local"
    assert parsed.timeout == 3
