"""The claim package's by-who rendering, restart-completed marker, idle snapshot and idle predicate."""

from unittest.mock import MagicMock

import psycopg
import pytest
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.state import AgentState
from agent.tests.claim.claim_support import _config, _make_runtime
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent


def test_by_who_self_returns_yourself():
    """source 'self' (ava.self.terminate/restart self-invocation) → 'yourself'.
    Lock down that the literal 'self' cannot be mutated to 'SELF' / 'XXselfXX' / '' etc. by mutmut."""
    from agent.graph.claim.node import _by_who

    assert _by_who("self") == "yourself"


def test_by_who_uppercase_self_passthrough():
    """source is case-sensitive — 'SELF' does not match the 'self' branch, falls back to
    return original value. `_by_who` does not do case-folding (to avoid mistakenly treating
    'Self' / 'SELF' as self-trigger)."""
    from agent.graph.claim.node import _by_who

    assert _by_who("SELF") == "SELF"
    assert _by_who("Self") == "Self"
    assert _by_who("SELF:UPDATE") == "SELF:UPDATE"


def test_by_who_external_source_passthrough():
    """Non self source ('user' / 'agent:42' / 'system')
    returns as-is — the marker text shows who triggered it at a glance."""
    from agent.graph.claim.node import _by_who

    assert _by_who("user") == "user"
    assert _by_who("user") == "user"
    assert _by_who("agent:42") == "agent:42"
    assert _by_who("system") == "system"


def test_by_who_self_prefix_does_not_match_self():
    """'self_xxx' / 'selfish' should not be recognized as 'self' (literal == comparison,
    not startswith)."""
    from agent.graph.claim.node import _by_who

    # anti-regression: changing to startswith("self") would make this test fail
    assert _by_who("selfish") == "selfish"
    assert _by_who("self_other") == "self_other"
    assert _by_who("self:other") == "self:other"  # only self is specifically handled


def test_render_restart_completed_marker_system_update_no_by_clause():
    """source='system:update' → 'updated and restarted' with no trailing 'by ...' noise."""
    from agent.graph.claim.node import _render_restart_completed_marker

    text = _render_restart_completed_marker("system:update")
    assert "updated and restarted" in text
    assert "by " not in text  # no actor suffix for system-driven rollout


def test_render_restart_completed_marker_plain_self_unchanged():
    """source='self' (ordinary restart, not update) → 'restarted by yourself', no 'updated'."""
    from agent.graph.claim.node import _render_restart_completed_marker

    text = _render_restart_completed_marker("self")
    assert "restarted by yourself" in text
    assert "updated" not in text


def test_render_restart_completed_marker_external_source_unchanged():
    """Non-update sources → plain 'restarted by <source>' wording."""
    from agent.graph.claim.node import _render_restart_completed_marker

    text = _render_restart_completed_marker("user")
    assert "restarted by user" in text
    assert "updated" not in text


async def test_claim_node_idle_enter_publishes_full_window_snapshot(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
):
    """Turn-end fallback: when claim is about to idle (no conversation yet),
    the wrapper must pass full_window=True so the enter snapshot is the full
    window — the only race-free view of the finished turn (reconnect GET can
    read a lagging checkpoint). Pins the will_idle wiring."""
    import json

    from langchain_core.messages import SystemMessage

    from agent.graph.claim import node as claim_node_mod
    from agent.graph.claim.node import claim_node

    tid = spawn_agent(catalog=model_catalog, authority=config_authority)

    # stub the body: we only exercise the wrapper + node_lifecycle enter path
    async def _stub_impl(_state, _runtime, _config):
        return Command(goto="end")

    monkeypatch.setattr(claim_node_mod, "_claim_node_impl", _stub_impl)  # pyright: ignore[reportUnknownArgumentType]

    pub = MagicMock()
    state = AgentState()
    state.messages = [SystemMessage(content="prompt")]
    await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )
    snaps = [
        json.loads(c.args[0]) for c in pub.emit.call_args_list if "timeline_snapshot" in c.args[0]
    ]
    assert len(snaps) == 1
    # will_idle=True (no conversation) → full-window: msg_count = full length,
    # window renders the whole (short) history — including the system-prompt
    # item (no Aw-Snap drop rule anymore: incremental snapshots never carry
    # 0.0 by construction, full-window ones are rare and the frontend's
    # id-replace merge keeps a single copy either way).
    assert snaps[0]["msg_count"] == 1
    assert [it["item_id"] for it in snaps[0]["items"]] == ["0.0"]
    assert snaps[0]["items"][0]["kind"] == "system_prompt"


def test_claim_will_idle_shares_the_impl_and_wrapper_contract() -> None:
    """One idle predicate for the impl branch and the wrapper's snapshot path:
    a trailing end-of-session note waives the fresh-window term (resume), and
    an open breaker parks idle (the wrapper previously omitted that arm)."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.graph.claim.node import claim_will_idle
    from agent.messages import system_note_message
    from agent.state_channels import CIRCUIT_REASON_BILLING, CircuitState
    from base.agents.messages.kwargs import NoteTag

    fresh = AgentState()
    fresh.messages = [SystemMessage(content="prompt")]
    assert claim_will_idle(fresh)  # no conversation yet: idle

    resume = AgentState()
    resume.impersonation_handoff_id = "7:0"
    note = system_note_message(
        content="session ended", tag=NoteTag.IMPERSONATION, created_at=datetime.now(UTC)
    )
    note.id = "impersonation-handoff:7:0"
    resume.messages = [SystemMessage(content="prompt"), note]
    assert not claim_will_idle(resume)  # the note must get its first turn

    resume.halted = True
    assert claim_will_idle(resume)  # an ended turn still wins

    parked = AgentState()
    parked.messages = [HumanMessage(content="hello")]
    parked.circuit = CircuitState(open=True, reason=CIRCUIT_REASON_BILLING)
    assert claim_will_idle(parked)
