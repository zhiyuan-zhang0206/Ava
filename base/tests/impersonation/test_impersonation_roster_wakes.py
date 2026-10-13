"""Impersonation wakes refresh the roster only when status changes."""

import pytest

from base.agents.impersonation import wake
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_announcements


def test_impersonation_wake_reconciles_roster_only_for_status_changes(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    timeline: list[int] = []
    roster: list[int] = []

    def no_wake(_db: Database, _bus: EventBus, _agent_id: int, _reason: str) -> None:
        pass

    monkeypatch.setattr(wake, "publish_inbound_wake", no_wake)
    patch_announcements(monkeypatch, wake, changed=timeline, updated=roster)
    wake.wake_agent(database, event_bus, 7)
    wake.wake_agent(database, event_bus, 7, roster_changed=True)
    assert timeline == [7, 7]
    assert roster == [7]
