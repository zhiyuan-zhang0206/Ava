"""The restarter's durable-disable boundary is enforced inside the daemon."""

from __future__ import annotations

import pytest

from ops import runner_mode
from services.restarter import daemon


def test_durably_disabled_restarter_exits_before_schema_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _schema_access(_url: str) -> None:
        pytest.fail("disabled restarter touched the database")

    monkeypatch.setattr(runner_mode, "is_hosted", lambda: False)
    monkeypatch.setattr(daemon, "read_skipped", lambda: {"restarter"})
    monkeypatch.setattr("shared.migrations.assert_schema_current", _schema_access)

    daemon.main()


def test_unreadable_disabled_marker_fails_closed_before_schema_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable() -> set[str]:
        raise OSError("marker unavailable")

    def _schema_access(_url: str) -> None:
        pytest.fail("restarter guessed permission to start")

    monkeypatch.setattr(runner_mode, "is_hosted", lambda: False)
    monkeypatch.setattr(daemon, "read_skipped", _unreadable)
    monkeypatch.setattr("shared.migrations.assert_schema_current", _schema_access)

    daemon.main()


def test_hosted_restarter_refuses_before_any_startup_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> set[str]:
        pytest.fail("hosted restarter reached process-mode startup")

    monkeypatch.setattr(runner_mode, "is_hosted", lambda: True)
    monkeypatch.setattr(daemon, "read_skipped", forbidden)
    daemon.main()


@pytest.mark.asyncio
async def test_hosted_dispatch_loop_never_runs_process_controllers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from unittest.mock import Mock

    async def forbidden_sleep(_delay: float) -> None:
        # CancelledError escapes the daemon's retry boundary, making the old
        # implementation fail promptly instead of hanging the negative proof.
        raise asyncio.CancelledError("hosted restarter entered the poll loop")

    monkeypatch.setattr(runner_mode, "is_hosted", lambda: True)
    monkeypatch.setattr(daemon.asyncio, "sleep", forbidden_sleep)
    pool = Mock()
    liveness = Mock()
    await daemon._dispatch_loop(pool, liveness)
    pool.connection.assert_not_called()
    liveness.beat.assert_not_called()


@pytest.mark.parametrize("controller_type", ["respawn", "resurrect", "wedged"])
def test_hosted_process_controller_is_inert(
    monkeypatch: pytest.MonkeyPatch, controller_type: str
) -> None:
    from unittest.mock import Mock

    from ops.controllers.respawn import RespawnController
    from ops.controllers.resurrect import CrashResurrectController
    from ops.controllers.wedged import WedgedAgentController

    def forbidden_gateway_probe() -> bool:
        pytest.fail("hosted process controller reached gateway I/O")

    monkeypatch.setattr("ops.controllers.resurrect._gateway_healthy", forbidden_gateway_probe)
    monkeypatch.setattr("ops.controllers.wedged._gateway_healthy", forbidden_gateway_probe)

    controllers = {
        "respawn": RespawnController,
        "resurrect": CrashResurrectController,
        "wedged": WedgedAgentController,
    }
    monkeypatch.setattr(runner_mode, "is_hosted", lambda: True)
    pool = Mock()
    result = controllers[controller_type](pool).reconcile("agent-runner")
    assert not result.acted
    pool.connection.assert_not_called()
