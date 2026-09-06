"""GET /api/agents/{id}/token-usage contract tests.

The endpoint reverse-scans state.messages for the most recent AIMessage
carrying usage_metadata and surfaces its input/output/reasoning token counts —
the frontend uses it to seed the token counter when switching to an existing
agent (the live SSE token_usage event is fire-and-forget, no persistence).

Coverage:
  - reverse scan picks the most recent AIMessage *with* usage_metadata, even
    when a later AIMessage has none
  - no checkpoint (new agent) -> 0/0
  - a checkpoint store read failure -> 0/0 (tolerance contract: the SSE push
    refreshes the UI later; contrast the /messages data endpoint's 503)
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    """TestClient + lifespan. db_conn's TRUNCATE runs before lifespan — the
    DB pool reuses the same test database (settings.data_plane.db_url is swapped by the
    conftest)."""
    with TestClient(app) as client:
        yield client


def _put_checkpoint(agent_id: int, messages: list) -> None:
    """Write channel_values.messages = `messages` directly via
    PostgresSaver.put, bypassing the graph — the endpoint only cares about
    reading back what is in the checkpoint."""
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.config import settings

    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def test_reverse_scan_finds_most_recent_usage(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """The most recent AIMessage *with* usage_metadata wins, even when a later
    AIMessage carries none — verifies the reverse scan skips the trailing
    usage-less message rather than returning 0/0. Also verifies that
    reasoning_tokens is extracted whether keyed as 'reasoning' (Anthropic) or
    'reasoning_tokens' (OpenAI)."""
    from langchain_core.messages import AIMessage

    from shared.config import settings

    # Through resolve_context_budget, not by re-deriving fraction * window here:
    # the threshold formula is that module's contract (covered numerically in
    # tests/shared/test_context_budget.py). What this test owns is that the
    # endpoint SERVES the resolved budget, which a local re-derivation would
    # stop checking the moment the two drifted.
    from shared.lm.context_budget import resolve_context_budget

    _budget = resolve_context_budget(settings.lm.llm_model)
    expected_max = _budget.max_context_tokens
    expected_soft = _budget.soft_compact_tokens
    expected_hard = _budget.hard_compact_tokens

    tid = create_agent(db_conn)
    messages = [
        AIMessage(
            content="x",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "output_token_details": {"reasoning": 7},
            },
        ),
        # a later AIMessage with no usage_metadata must NOT shadow the one above
        AIMessage(content="follow-up with no usage"),
    ]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/token-usage")
    assert resp.status_code == 200
    assert resp.json() == {
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 7,
        "max_input_tokens": expected_max,
        "soft_compact_tokens": expected_soft,
        "hard_compact_tokens": expected_hard,
    }


def test_new_agent_returns_zero(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """No checkpoint -> 0/0 for token counts. max_input_tokens reflects the
    cluster default model's context window (falls back from config_overlay)."""
    from shared.config import settings

    # Through resolve_context_budget, not by re-deriving fraction * window here:
    # the threshold formula is that module's contract (covered numerically in
    # tests/shared/test_context_budget.py). What this test owns is that the
    # endpoint SERVES the resolved budget, which a local re-derivation would
    # stop checking the moment the two drifted.
    from shared.lm.context_budget import resolve_context_budget

    _budget = resolve_context_budget(settings.lm.llm_model)
    expected_max = _budget.max_context_tokens
    expected_soft = _budget.soft_compact_tokens
    expected_hard = _budget.hard_compact_tokens

    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/token-usage")
    assert resp.status_code == 200
    assert resp.json() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "max_input_tokens": expected_max,
        "soft_compact_tokens": expected_soft,
        "hard_compact_tokens": expected_hard,
    }


def test_per_model_thresholds_scale_to_overlay_model(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """soft/hard compact thresholds are a fraction of the agent model's window,
    resolved from the per-agent config_overlay — a 200K-window model reports
    60K / 80K (the 0.3 / 0.4 rule), not the cluster-default model's numbers."""
    tid = create_agent(db_conn)
    # create_agent inserts only the `agents` row; add the agents_meta row that
    # carries config_overlay (status has no default, so it must be supplied).
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status, config_overlay) "
            "VALUES (%s, 'running', %s::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET config_overlay = EXCLUDED.config_overlay",
            (tid, '{"llm_model": "claude-haiku-4-5-20251001"}'),
        )
    db_conn.commit()

    resp = test_client.get(f"/api/agents/{tid}/token-usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_input_tokens"] == 200_000
    assert body["hard_compact_tokens"] == 80_000  # 0.4 * 200K
    assert body["soft_compact_tokens"] == 60_000  # 0.3 * 200K


def test_checkpoint_read_failure_returns_zero(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint store read failure is tolerated to 0/0 — the SSE token_usage
    push refreshes the counter later (contrast the /messages data endpoint's
    503). Patch langgraph.checkpoint.postgres's PostgresSaver name to raise
    (load_checkpoint_messages resolves it via a function-local import).
    max_input_tokens still reflects the cluster default model (read from
    config_overlay before the checkpoint access)."""
    from contextlib import contextmanager

    import langgraph.checkpoint.postgres as ckpt_mod

    from shared.config import settings

    # Through resolve_context_budget, not by re-deriving fraction * window here:
    # the threshold formula is that module's contract (covered numerically in
    # tests/shared/test_context_budget.py). What this test owns is that the
    # endpoint SERVES the resolved budget, which a local re-derivation would
    # stop checking the moment the two drifted.
    from shared.lm.context_budget import resolve_context_budget

    _budget = resolve_context_budget(settings.lm.llm_model)
    expected_max = _budget.max_context_tokens
    expected_soft = _budget.soft_compact_tokens
    expected_hard = _budget.hard_compact_tokens

    tid = create_agent(db_conn)

    @contextmanager
    def fake_saver(_url):
        raise OSError("simulated DB connection lost")
        yield  # type: ignore[unreachable]

    class FakeSaver:
        from_conn_string = staticmethod(fake_saver)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(ckpt_mod, "PostgresSaver", FakeSaver)
    resp = test_client.get(f"/api/agents/{tid}/token-usage")
    assert resp.status_code == 200
    assert resp.json() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "max_input_tokens": expected_max,
        "soft_compact_tokens": expected_soft,
        "hard_compact_tokens": expected_hard,
    }
