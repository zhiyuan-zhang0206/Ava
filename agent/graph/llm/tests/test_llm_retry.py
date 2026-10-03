"""The llm node's own retry loop (agent/graph/llm/_retry.py): which failures retry, how long each
retry waits, and the loop that applies it.

The agent host builds ONE graph for every local agent, so the schedule is decided per failure from
the agent's model and id, never from a graph-level policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langgraph.runtime import Runtime

from agent.graph.llm import _retry, node
from agent.graph.llm._retry import (
    RETRY_JITTER_SPAN_S,
    RETRY_REMAINING_ATTR,
    Attempt,
    retry_phase_jitter,
    retry_wait,
)
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LLMStreamStallPairError,
    LLMStreamStallPairExhaustedError,
    LLMStreamStallTimeoutError,
    _check_stall_pair_cap,
    _record_stall_pair_streak,
    _reset_stall_pair_streak,
    _stall_pair_streak,
)
from agent.hooks.compact import CompactionFailedError
from base.agents.context import AvaContext
from base.config import settings
from base.host.env.agent_slices import AgentSlices

_MODEL = "deepseek-flash"
_ZERO_PHASE = 1000  # agent_id % 1000 == 0: the transient schedule starts unshifted


def _wait(exc: Exception, attempts: int = 1, agent_id: int = _ZERO_PHASE) -> float | None:
    return retry_wait(exc, attempts, model=_MODEL, agent_id=agent_id)


def _fixed_cap(cap: int) -> Callable[..., int]:
    """A `resolve_setting` stand-in giving every model the same try cap."""

    def resolve(_name: str, *, model: str) -> int:
        del model
        return cap

    return resolve


@pytest.fixture
def streak_agent() -> Iterator[int]:
    """An agent id whose stall-pair streak is cleaned up after the test."""
    _reset_stall_pair_streak("6363")
    yield 6363
    _reset_stall_pair_streak("6363")


def test_fatal_and_compaction_failures_are_never_retried() -> None:
    for exc in (
        FatalLLMStreamError("cap exhausted"),
        FatalProviderError("out of balance"),
        CompactionFailedError("summary retry budget exhausted"),
    ):
        assert _wait(exc) is None, type(exc).__name__


def test_other_failures_are_retried() -> None:
    assert _wait(ConnectionError("network")) is not None
    assert _wait(LLMStreamStallTimeoutError("stall")) is not None


def test_transient_waits_double_from_the_initial_interval_plus_up_to_a_second_of_jitter() -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for attempts in (1, 2, 3):
        wait = _wait(ConnectionError("x"), attempts)
        assert wait is not None
        base = min(initial * 2 ** (attempts - 1), settings.lm.llm_retry_max_interval_seconds)
        assert base <= wait < base + 1.0, attempts


def test_the_model_caps_the_number_of_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_resolve(_name: str, *, model: str) -> int:
        seen.append(model)
        return 3

    monkeypatch.setattr("base.lm.registry.resolve_setting", fake_resolve)
    exc = ConnectionError("x")
    assert retry_wait(exc, 2, model="model-for-this-agent", agent_id=1) is not None
    assert retry_wait(exc, 3, model="model-for-this-agent", agent_id=1) is None
    assert set(seen) == {"model-for-this-agent"}


def test_a_spent_total_budget_ends_the_retries() -> None:
    exc = ConnectionError("budget exhausted")
    setattr(exc, RETRY_REMAINING_ATTR, 0.0)
    assert _wait(exc) is None


def test_the_last_wait_is_clipped_to_the_remaining_budget() -> None:
    """With 1.5s left one second is reserved for the jitter the wait carries, so the base wait is
    0.5s and the whole sleep stays inside the budget; at or under a second left there is no
    jitter and the wait is what remains."""
    exc = ConnectionError("retryable")
    setattr(exc, RETRY_REMAINING_ATTR, 1.5)
    wait = _wait(exc)
    assert wait is not None
    assert 0.5 <= wait < 1.5

    tight = ConnectionError("retryable")
    setattr(tight, RETRY_REMAINING_ATTR, 0.8)
    assert _wait(tight) == 0.8


# --- retry de-phasing (task #960) ---


def test_the_phase_offset_is_stable_per_agent_and_differs_between_agents() -> None:
    assert retry_phase_jitter(11) == retry_phase_jitter(11)
    assert retry_phase_jitter(11) != retry_phase_jitter(22)
    for agent_id in (1, 7, 999, 1000, 123456):
        assert 0.0 <= retry_phase_jitter(agent_id) < RETRY_JITTER_SPAN_S


def test_the_transient_schedule_starts_at_the_agents_phase() -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for agent_id in (11, 22):
        wait = _wait(ConnectionError("x"), agent_id=agent_id)
        assert wait is not None
        start = initial + retry_phase_jitter(agent_id)
        assert start <= wait < start + 1.0


# --- delayed stall schedule (task #3884) ---


def test_a_stall_pair_gets_the_delayed_schedule(streak_agent: int) -> None:
    """Minutes-scale jittered wait, no compounding, its own attempts headroom."""
    wait = _wait(LLMStreamStallPairError("pair", stage="ttft"), agent_id=streak_agent)
    assert wait is not None
    assert 300.0 * 0.75 <= wait <= 300.0 * 1.25  # initial 5min, jittered +-25%
    assert _stall_pair_streak(str(streak_agent)) == 1


def test_stall_pair_waits_double_and_cap(streak_agent: int) -> None:
    for streak, base in {1: 300.0, 2: 600.0, 3: 1200.0, 4: 1800.0}.items():
        wait = _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent)
        assert wait is not None
        assert base * 0.75 <= wait <= base * 1.25, f"streak {streak}"


def test_a_stall_pair_past_the_cap_is_refused_and_the_streak_resets(streak_agent: int) -> None:
    _record_stall_pair_streak(str(streak_agent), settings.lm.llm_stall_retry_max_consecutive)
    assert _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent) is None
    assert _stall_pair_streak(str(streak_agent)) == 0


def test_the_stall_pair_headroom_extends_the_transient_attempts_gate(
    streak_agent: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.lm.registry.resolve_setting", _fixed_cap(2))
    pairs = settings.lm.llm_stall_retry_max_consecutive
    pair = LLMStreamStallPairError("pair")
    assert retry_wait(pair, 2 + pairs - 1, model=_MODEL, agent_id=streak_agent) is not None
    assert retry_wait(pair, 2 + pairs, model=_MODEL, agent_id=streak_agent) is None


def test_the_delayed_schedule_can_be_disabled(
    streak_agent: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`llm_stall_retry_max_consecutive=0`: pair errors retry (or stop) like any transient error,
    with no streak."""
    monkeypatch.setattr(settings.lm, "llm_stall_retry_max_consecutive", 0)
    initial = settings.lm.llm_retry_initial_interval_seconds
    wait = _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent)
    assert wait is not None
    start = initial + retry_phase_jitter(streak_agent)
    assert start <= wait < start + 1.0
    assert _stall_pair_streak(str(streak_agent)) == 0

    exhausted = LLMStreamStallPairError("pair")
    setattr(exhausted, RETRY_REMAINING_ATTR, 0.0)
    assert _wait(exhausted, agent_id=streak_agent) is None


def test_a_spent_streak_fails_the_turn_at_node_entry_and_resets(streak_agent: int) -> None:
    _record_stall_pair_streak(str(streak_agent), settings.lm.llm_stall_retry_max_consecutive)
    with pytest.raises(LLMStreamStallPairExhaustedError):
        _check_stall_pair_cap(str(streak_agent))
    assert _stall_pair_streak(str(streak_agent)) == 0
    assert isinstance(LLMStreamStallPairExhaustedError("x"), FatalLLMStreamError)
    assert isinstance(LLMStreamStallPairError("x"), LLMStreamStallTimeoutError)


# --- the loop ---


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=None, llm=MagicMock(), event_publisher=MagicMock(), agent=AgentSlices.resolve()
    )
    return Runtime(context=ctx)


def _drive(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[object]
) -> tuple[list[Attempt], list[float], dict[str, Any]]:
    """Run `llm_node` over scripted per-try outcomes (an exception is raised, anything else is
    returned); returns the tries seen, the sleeps taken and the node's result or error."""
    tries: list[Attempt] = []
    sleeps: list[float] = []
    result: dict[str, Any] = {}

    async def fake_attempt(_state: object, _runtime: object, _config: object, attempt: Attempt):
        tries.append(attempt)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(node, "llm_attempt", fake_attempt)
    monkeypatch.setattr(node.asyncio, "sleep", fake_sleep)

    async def run() -> None:
        try:
            result["ok"] = await node.llm_node(
                cast(Any, object()), _runtime(), {"configurable": {"thread_id": "1000"}}
            )
        except Exception as exc:
            result["error"] = exc

    asyncio.run(run())
    return tries, sleeps, result


def test_the_node_retries_a_transient_failure_and_returns_the_next_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tries, sleeps, result = _drive(monkeypatch, [ConnectionError("a"), ConnectionError("b"), "ok"])

    assert result == {"ok": "ok"}
    assert [t.number for t in tries] == [1, 2, 3]
    assert len({t.first_started_at for t in tries}) == 1  # one clock for the whole node
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # the second wait doubles


def test_the_node_does_not_retry_a_fatal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tries, sleeps, result = _drive(monkeypatch, [FatalProviderError("balance"), "never"])

    assert isinstance(result["error"], FatalProviderError)
    assert len(tries) == 1 and sleeps == []


def test_the_node_gives_up_at_the_models_attempt_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_retry, "_NEVER_RETRIED", ())
    monkeypatch.setattr("base.lm.registry.resolve_setting", _fixed_cap(3))
    tries, sleeps, result = _drive(monkeypatch, [ConnectionError(str(i)) for i in range(5)])

    assert str(result["error"]) == "2"  # the third failed try ends the node
    assert len(tries) == 3 and len(sleeps) == 2
