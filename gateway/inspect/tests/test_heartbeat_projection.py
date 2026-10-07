"""Heartbeat projection arithmetic belongs to the inspector read model."""

from datetime import UTC, datetime, timedelta

import pytest

from base.agents.contract import AgentStatus
from base.config import settings
from gateway.inspect import _live


def test_zero_jitter_span_disables_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled jitter never performs modulo zero and leaves the exact idle due time."""
    monkeypatch.setattr(_live, "JITTER_SPAN_S", 0)
    last_active_at = datetime(2026, 1, 1, tzinfo=UTC)
    heartbeat = _live.project_heartbeat(
        AgentStatus.IDLING,
        last_active_at,
        None,
        None,
        agent_id=17,
        pending_inbound=False,
        last_pause=None,
    )
    assert heartbeat.paused_until is None
    assert heartbeat.heartbeat_pending is False
    assert heartbeat.next_at == last_active_at + timedelta(
        seconds=settings.daemon.heartbeat_idle_threshold_seconds
    )
