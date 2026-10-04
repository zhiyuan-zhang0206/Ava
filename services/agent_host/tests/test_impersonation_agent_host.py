"""A held agent's host wake returns before any runtime or slot, refuses an unaccepted control batch and supervises the active lease's relay."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from base.native_process.runtime_incarnation import RuntimeIncarnation


def _session(status: str = "active", **values: Any) -> dict[str, Any]:
    return {
        "id": "lease-1",
        "automatic": False,
        "handoff_applied_at": None,
        "process_metadata": {},
        "source": "external_agent:codex:task1",
        "status": status,
        "reason": "Finish the assigned task",
        "consent_version": 1,
        "plugin_delta": [],
        "delta_version": 0,
        "applied_version": 0,
        "relay_provider": "codex",
        "relay_thread_id": "thread-1",
        "relay_codex_remote": None,
        "relay_heartbeat_at": datetime.now(UTC),
        "relay_last_failure_at": None,
        **values,
    }


async def test_held_host_wake_returns_before_runtime_or_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent_host.host import AgentHost
    from services.agent_host.runtime import _StoredConfig

    host = object.__new__(AgentHost)
    host._machine = "local"
    host._owner = uuid4()
    host._maintenance_failed = {}
    host.turn_fingerprints = {}
    host._control_pool = MagicMock()
    host._db = MagicMock()
    host._bus = MagicMock()
    host._checkpointer = cast(Any, MemorySaver())
    host._read_stored_config = AsyncMock(
        return_value=_StoredConfig(
            machine="local", status="idling", config_overlay=None, birth_config=None
        )
    )
    host._runtime_for = AsyncMock()
    monkeypatch.setattr("services.agent_host.host.active_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("services.agent_host.host.native_status", AsyncMock(return_value=None))
    monkeypatch.setattr("services.agent_host.host.supervise_relay", AsyncMock())
    monkeypatch.setattr("agent.db.claim_inbound_batch", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "services.agent_host.host.apply_hosted_lifecycle", AsyncMock(return_value=None)
    )
    admission = AsyncMock(return_value=RuntimeIncarnation(42, uuid4(), host._owner))
    settlement = AsyncMock(return_value=True)
    monkeypatch.setattr("services.agent_host.host.admit_hosted_runtime", admission)
    monkeypatch.setattr("services.agent_host.host.settle_hosted_runtime", settlement)
    # No admission slot or graph exists: touching either is a test failure.
    await host._run_turn(42)
    host._runtime_for.assert_not_awaited()
    admission.assert_awaited_once()
    settlement.assert_awaited_once()


async def test_held_host_refuses_unaccepted_control_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.agent_host.host import AgentHost

    host = object.__new__(AgentHost)
    host._machine = "local"
    host._owner = uuid4()
    host._control_pool = MagicMock()
    host._db = MagicMock()
    host._bus = MagicMock()
    host._checkpointer = cast(Any, MemorySaver())
    owner = RuntimeIncarnation(42, uuid4(), host._owner)
    monkeypatch.setattr(
        "services.agent_host.host.admit_hosted_runtime", AsyncMock(return_value=owner)
    )
    monkeypatch.setattr("services.agent_host.host.native_status", AsyncMock(return_value=None))
    monkeypatch.setattr("services.agent_host.host.supervise_relay", AsyncMock())
    monkeypatch.setattr(
        "agent.db.claim_inbound_batch",
        AsyncMock(return_value=[SimpleNamespace(durable_lifecycle=False)]),
    )
    apply = AsyncMock(return_value=None)
    monkeypatch.setattr("services.agent_host.host.apply_hosted_lifecycle", apply)
    monkeypatch.setattr("services.agent_host.host.settle_hosted_runtime", AsyncMock())
    with pytest.raises(RuntimeError, match="held control claim returned an unaccepted command"):
        await host._run_held_controls(42, "idling")
    apply.assert_not_awaited()


async def test_held_controls_supervise_the_active_lease_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim gate never runs during an active lease; the held-controls pass
    must carry the relay supervision (task #2634)."""
    from services.agent_host.host import AgentHost

    host = object.__new__(AgentHost)
    host._machine = "local"
    host._owner = uuid4()
    host._control_pool = MagicMock()
    host._db = MagicMock()
    host._bus = MagicMock()
    host._checkpointer = cast(Any, MemorySaver())
    owner = RuntimeIncarnation(42, uuid4(), host._owner)
    monkeypatch.setattr(
        "services.agent_host.host.admit_hosted_runtime", AsyncMock(return_value=owner)
    )
    session = _relay_session("active")
    monkeypatch.setattr("services.agent_host.host.native_status", AsyncMock(return_value=session))
    supervise = AsyncMock()
    monkeypatch.setattr("services.agent_host.host.supervise_relay", supervise)
    monkeypatch.setattr("agent.db.claim_inbound_batch", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "services.agent_host.host.apply_hosted_lifecycle", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("services.agent_host.host.settle_hosted_runtime", AsyncMock())
    await host._run_held_controls(42, "idling")
    supervise.assert_awaited_once_with(host._db, host._bus, session, 42)


def _relay_session(
    status: str = "accepted", *, provider: str = "codex", **values: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "relay_provider": provider,
        "relay_thread_id": "thread-1" if provider == "codex" else None,
        "relay_codex_remote": None,
        "relay_heartbeat_at": None,
        "relay_last_failure_at": None,
    }
    base.update(values)
    return _session(status, **base)
