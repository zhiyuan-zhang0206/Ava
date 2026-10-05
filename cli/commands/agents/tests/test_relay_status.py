"""CLI relay wire status restores the domain owner before terminal dispatch."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.agents import impersonation_relay as relay

LEASE_ID = UUID("767fb040-aa54-42ae-b2c8-594039fbbf46")


@pytest.mark.parametrize("status", list(ImpersonationStatus))
def test_wire_lease_status_restores_shared_enum(
    monkeypatch: pytest.MonkeyPatch,
    status: ImpersonationStatus,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.agents import impersonation

    def get(_db: object, _bus: object, _lease_id: str, _token: str) -> dict[str, Any]:
        return {
            "id": str(LEASE_ID),
            "agent_id": 42,
            "status": status.value,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "ack_window_seconds": 180,
            "max_delivery_attempts": 2,
        }

    monkeypatch.setattr(impersonation, "relay_get", get)

    def inbox(_db: object, _lease_id: str, _token: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    assert (
        relay._Lease.model_validate(get(database, event_bus, str(LEASE_ID), "test-token")).status
        is status
    )
    snapshot = relay._read_inbox(database, event_bus, 42, LEASE_ID, "test-token")
    assert snapshot.status is status
    assert snapshot.active is (status is ImpersonationStatus.ACTIVE)


@pytest.mark.parametrize("status", [ImpersonationStatus.REQUESTED, ImpersonationStatus.ACCEPTED])
def test_pending_consent_checks_status_without_opening_inbox(
    monkeypatch: pytest.MonkeyPatch,
    status: ImpersonationStatus,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.agents import impersonation

    def get(_db: object, _bus: object, _lease_id: str, _token: str) -> dict[str, Any]:
        return {
            "id": str(LEASE_ID),
            "agent_id": 42,
            "status": status,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "ack_window_seconds": 180,
            "max_delivery_attempts": 2,
        }

    def inbox(_db: object, _lease_id: str, _token: str) -> list[dict[str, Any]]:
        pytest.fail("Pending consent must not read the protected inbox")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    snapshot = relay._read_inbox(database, event_bus, 42, LEASE_ID, "test-token")
    assert snapshot.status == status
    assert not snapshot.message_ids
