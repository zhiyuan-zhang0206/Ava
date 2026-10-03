"""The claim heartbeat runs the page reconcile."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent.state import AgentState


async def test_heartbeat_runs_page_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEARTBEAT handler probes the agent's pages (Task #973: live agents must
    self-heal pages killed by a cluster rollout — boot recovery never runs)."""
    from agent.db import ClaimedInbound
    from agent.graph.claim.node import _BatchState, _handle_heartbeat

    calls: list[tuple[object, int, object | None]] = []

    async def _fake_reconcile(pool, agent_id, *, event_publisher, db: object, bus: object):
        calls.append((pool, agent_id, event_publisher))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _fake_reconcile)  # pyright: ignore[reportUnknownArgumentType]

    class _Ctx:
        ops_pool = object()
        event_publisher = object()

        def require_db(self) -> object:
            return object()

        def require_bus(self) -> object:
            return object()

    item = ClaimedInbound(id=1, agent_id=7, content="check-in", kind="heartbeat", source="system")
    st = _BatchState()
    state = AgentState(messages=[HumanMessage(content="hi")])
    await _handle_heartbeat(_Ctx(), 7, item, st, state)  # type: ignore[arg-type]

    assert len(calls) == 1
    _pool, agent_id, publisher = calls[0]
    assert agent_id == 7
    assert publisher is _Ctx.event_publisher
    # heartbeat system note still appended as usual (breaker closed)
    assert len(st.new_msgs) == 1
