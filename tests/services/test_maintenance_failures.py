"""A failed continuation cannot become drained after journal I/O recovers."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from shared import maintenance, pause_owner
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate
from tests.services.test_agent_host import _Build, _Row
from tests.services.test_agent_host import host_plugin as host_plugin
from tests.services.test_agent_host import wired as wired


@pytest.mark.parametrize("broken_io", ["read", "write"])
async def test_failure_is_latched_before_any_journal_io(
    wired: _Build,
    monkeypatch: pytest.MonkeyPatch,
    broken_io: str,
) -> None:
    host, graph, _pool = wired({11: _Row(status="idling")})
    real_snapshot = maintenance.snapshot
    fail_next_read = False

    def read() -> pause_owner.PauseOwnerSnapshot | None:
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise RuntimeError("isolated journal read failure")
        return real_snapshot()

    def write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("isolated journal write failure")

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        nonlocal fail_next_read
        before = pause_owner.begin_maintenance("failed", WHEN)
        assert before.maintenance is not None
        pause_owner.change_maintenance(
            "failed", WHEN, before.maintenance, MaintenanceHold("draining", {11: 100})
        )
        fail_next_read = broken_io == "read"
        raise RuntimeError("isolated final flush failure")

    monkeypatch.setattr(maintenance, "snapshot", read)
    if broken_io == "write":
        monkeypatch.setattr(maintenance, "record_failure", write)
    monkeypatch.setattr(graph, "ainvoke", broken)
    with pytest.raises((RuntimeError, OSError), match="journal"):
        await host.run_turn(11)
    assert maintenance.pending_command(11) == 100
    control = AsyncMock()
    monkeypatch.setattr(host, "_run_held_controls", control)
    await host.run_turn(11)
    control.assert_not_awaited()
