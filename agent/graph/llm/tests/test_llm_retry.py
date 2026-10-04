"""The llm node's own retry loop (agent/graph/llm/_retry.py): which failures retry, how long each
retry waits, and the loop that applies it.

The agent host builds ONE graph for every local agent, so the schedule is decided per failure from
the agent's model and id, never from a graph-level policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables import RunnableConfig
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
    LlmLedger,
    LLMStreamStallPairError,
    LLMStreamStallPairExhaustedError,
    LLMStreamStallTimeoutError,
)
from agent.hooks.compact import CompactionFailedError
from base.agents.context import AvaContext
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices

_MODEL = "deepseek-flash"
_ZERO_PHASE = 1000  # agent_id % 1000 == 0: the transient schedule starts unshifted


def _wait(
    exc: Exception, attempts: int = 1, agent_id: int = _ZERO_PHASE, *, ledger: LlmLedger
) -> float | None:
    return retry_wait(exc, attempts, model=_MODEL, agent_id=agent_id, ledger=ledger)


def _uniform_low(low: float, _high: float) -> float:
    return low


def _uniform_high(_low: float, high: float) -> float:
    return high


def _fixed_delay(_streak: int) -> float:
    return 300.0


def _caps(by_model: dict[str, int]) -> Callable[..., int]:
    """A `resolve_setting` stand-in giving each model its own try cap."""

    def resolve(_name: str, *, model: str) -> int:
        return by_model[model]

    return resolve


def _fixed_cap(cap: int) -> Callable[..., int]:
    """A `resolve_setting` stand-in giving every model the same try cap."""

    def resolve(_name: str, *, model: str) -> int:
        del model
        return cap

    return resolve


@pytest.fixture
def ledger() -> LlmLedger:
    """A fresh ledger per test: the streaks and error counts start empty."""
    return LlmLedger()


@pytest.fixture
def streak_agent() -> int:
    """The agent id the stall-pair tests run as."""
    return 6363


def test_fatal_and_compaction_failures_are_never_retried(ledger: LlmLedger) -> None:
    for exc in (
        FatalLLMStreamError("cap exhausted"),
        FatalProviderError("out of balance"),
        CompactionFailedError("summary retry budget exhausted"),
    ):
        assert _wait(exc, ledger=ledger) is None, type(exc).__name__


def test_other_failures_are_retried(ledger: LlmLedger) -> None:
    assert _wait(ConnectionError("network"), ledger=ledger) is not None
    assert _wait(LLMStreamStallTimeoutError("stall"), ledger=ledger) is not None


def test_transient_waits_double_from_the_initial_interval_plus_up_to_a_second_of_jitter(
    ledger: LlmLedger,
) -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for attempts in (1, 2, 3):
        wait = _wait(ConnectionError("x"), attempts, ledger=ledger)
        assert wait is not None
        base = min(initial * 2 ** (attempts - 1), settings.lm.llm_retry_max_interval_seconds)
        assert base <= wait < base + 1.0, attempts


def test_the_model_caps_the_number_of_tries(
    monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    seen: list[str] = []

    def fake_resolve(_name: str, *, model: str) -> int:
        seen.append(model)
        return 3

    monkeypatch.setattr("base.lm.registry.resolve_setting", fake_resolve)
    exc = ConnectionError("x")
    assert retry_wait(exc, 2, model="model-for-this-agent", agent_id=1, ledger=ledger) is not None
    assert retry_wait(exc, 3, model="model-for-this-agent", agent_id=1, ledger=ledger) is None
    assert set(seen) == {"model-for-this-agent"}


def test_a_spent_total_budget_ends_the_retries(ledger: LlmLedger) -> None:
    from base.config.domains.lm import LmSettings

    assert LmSettings().llm_retry_max_total_seconds == 420.0
    exc = ConnectionError("budget exhausted")
    setattr(exc, RETRY_REMAINING_ATTR, 0.0)
    assert _wait(exc, ledger=ledger) is None


def test_the_clipped_wait_is_exactly_the_clipped_base_plus_the_jitter(
    monkeypatch: pytest.MonkeyPatch,
    ledger: LlmLedger,
) -> None:
    """The former policy's `max_interval == 0.5` with jitter on, at 1.5s left: the base wait is
    0.5s and the jitter (0..1s) rides on top."""
    exc = ConnectionError("retryable")
    setattr(exc, RETRY_REMAINING_ATTR, 1.5)
    monkeypatch.setattr(_retry.random, "uniform", _uniform_low)
    assert _wait(exc, ledger=ledger) == 0.5
    monkeypatch.setattr(_retry.random, "uniform", _uniform_high)
    assert _wait(exc, ledger=ledger) == 1.5


def test_the_last_wait_is_clipped_to_the_remaining_budget(ledger: LlmLedger) -> None:
    """With 1.5s left one second is reserved for the jitter the wait carries, so the base wait is
    0.5s and the whole sleep stays inside the budget; at or under a second left there is no
    jitter and the wait is what remains."""
    exc = ConnectionError("retryable")
    setattr(exc, RETRY_REMAINING_ATTR, 1.5)
    wait = _wait(exc, ledger=ledger)
    assert wait is not None
    assert 0.5 <= wait < 1.5

    tight = ConnectionError("retryable")
    setattr(tight, RETRY_REMAINING_ATTR, 0.8)
    assert _wait(tight, ledger=ledger) == 0.8


# --- retry de-phasing (task #960) ---


def test_the_phase_offset_is_stable_per_agent_and_differs_between_agents() -> None:
    assert retry_phase_jitter(11) == retry_phase_jitter(11)
    assert retry_phase_jitter(11) != retry_phase_jitter(22)
    for agent_id in (1, 7, 999, 1000, 123456):
        assert 0.0 <= retry_phase_jitter(agent_id) < RETRY_JITTER_SPAN_S


def test_the_phase_offsets_of_two_named_agents(ledger: LlmLedger) -> None:
    """The former policy's pair: agent 1234 and 5678 get stable, different offsets in the span."""
    first, second = retry_phase_jitter(1234), retry_phase_jitter(5678)
    assert first == RETRY_JITTER_SPAN_S * 234 / 1000 and second == RETRY_JITTER_SPAN_S * 678 / 1000
    assert retry_phase_jitter(1000) == 0.0  # no offset: the schedule is exactly the configured one
    initial = settings.lm.llm_retry_initial_interval_seconds
    wait = _wait(ConnectionError("x"), agent_id=1000, ledger=ledger)
    assert wait is not None and initial <= wait < initial + 1.0


def test_the_transient_schedule_starts_at_the_agents_phase(ledger: LlmLedger) -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for agent_id in (11, 22):
        wait = _wait(ConnectionError("x"), agent_id=agent_id, ledger=ledger)
        assert wait is not None
        start = initial + retry_phase_jitter(agent_id)
        assert start <= wait < start + 1.0


# --- delayed stall schedule (task #3884) ---


def test_a_stall_pair_gets_the_delayed_schedule(streak_agent: int, ledger: LlmLedger) -> None:
    """Minutes-scale jittered wait, no compounding, its own attempts headroom."""
    wait = _wait(
        LLMStreamStallPairError("pair", stage="ttft"), agent_id=streak_agent, ledger=ledger
    )
    assert wait is not None
    assert 300.0 * 0.75 <= wait <= 300.0 * 1.25  # initial 5min, jittered +-25%
    assert ledger.stall_pair_streak(str(streak_agent)) == 1


def test_the_delayed_wait_is_served_whole(
    streak_agent: int, monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    """No backoff compounding and no extra additive jitter on top of the delayed wait (the former
    policy's backoff 1.0 / jitter off / max_interval == sleep)."""
    monkeypatch.setattr(_retry, "delayed_stall_sleep", _fixed_delay)
    assert _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger) == 300.0
    assert _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger) == 300.0


def test_stall_pair_streaks_are_kept_per_agent(streak_agent: int, ledger: LlmLedger) -> None:
    """One host process serves every agent: pair streaks must not share a bucket."""
    other = streak_agent + 1
    _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger)
    _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger)
    _wait(LLMStreamStallPairError("pair"), agent_id=other, ledger=ledger)
    assert ledger.stall_pair_streak(str(streak_agent)) == 2
    assert ledger.stall_pair_streak(str(other)) == 1


def test_stall_pair_errors_skip_the_consecutive_tracker(
    streak_agent: int, ledger: LlmLedger
) -> None:
    """The tracker's cap (3) must not pre-empt the delayed schedule's 4th grant, so pair errors
    never enter it; plain stalls still do."""
    thread = str(streak_agent)
    ledger.record_consecutive_error(thread, LLMStreamStallPairError("pair"))
    assert ledger.consecutive_error(thread) is None
    ledger.record_consecutive_error(thread, LLMStreamStallTimeoutError("stall"))
    assert ledger.consecutive_error(thread) == ("LLMStreamStallTimeoutError", 1)


def test_stall_pair_waits_double_and_cap(streak_agent: int, ledger: LlmLedger) -> None:
    for streak, base in {1: 300.0, 2: 600.0, 3: 1200.0, 4: 1800.0}.items():
        wait = _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger)
        assert wait is not None
        assert base * 0.75 <= wait <= base * 1.25, f"streak {streak}"


def test_a_stall_pair_past_the_cap_is_refused_and_the_streak_resets(
    streak_agent: int, ledger: LlmLedger
) -> None:
    ledger.record_stall_pair_streak(str(streak_agent), settings.lm.llm_stall_retry_max_consecutive)
    assert _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger) is None
    assert ledger.stall_pair_streak(str(streak_agent)) == 0


def test_the_stall_pair_headroom_extends_the_transient_attempts_gate(
    streak_agent: int, monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    monkeypatch.setattr("base.lm.registry.resolve_setting", _fixed_cap(2))
    pairs = settings.lm.llm_stall_retry_max_consecutive
    pair = LLMStreamStallPairError("pair")
    assert (
        retry_wait(pair, 2 + pairs - 1, model=_MODEL, agent_id=streak_agent, ledger=ledger)
        is not None
    )
    assert retry_wait(pair, 2 + pairs, model=_MODEL, agent_id=streak_agent, ledger=ledger) is None


def test_the_delayed_schedule_can_be_disabled(
    streak_agent: int, monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    """`llm_stall_retry_max_consecutive=0`: pair errors retry (or stop) like any transient error,
    with no streak."""
    monkeypatch.setattr(settings.lm, "llm_stall_retry_max_consecutive", 0)
    initial = settings.lm.llm_retry_initial_interval_seconds
    wait = _wait(LLMStreamStallPairError("pair"), agent_id=streak_agent, ledger=ledger)
    assert wait is not None
    start = initial + retry_phase_jitter(streak_agent)
    assert start <= wait < start + 1.0
    assert ledger.stall_pair_streak(str(streak_agent)) == 0

    exhausted = LLMStreamStallPairError("pair")
    setattr(exhausted, RETRY_REMAINING_ATTR, 0.0)
    assert _wait(exhausted, agent_id=streak_agent, ledger=ledger) is None


def test_a_spent_streak_fails_the_turn_at_node_entry_and_resets(
    streak_agent: int, ledger: LlmLedger
) -> None:
    ledger.record_stall_pair_streak(str(streak_agent), settings.lm.llm_stall_retry_max_consecutive)
    with pytest.raises(LLMStreamStallPairExhaustedError):
        ledger.check_stall_pair_cap(str(streak_agent))
    assert ledger.stall_pair_streak(str(streak_agent)) == 0
    assert isinstance(LLMStreamStallPairExhaustedError("x"), FatalLLMStreamError)
    assert isinstance(LLMStreamStallPairError("x"), LLMStreamStallTimeoutError)


# --- the loop ---


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
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

    async def fake_attempt(
        _state: object, _runtime: object, _config: object, attempt: Attempt, _ledger: object
    ):
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
                cast(Any, object()),
                _runtime(),
                {"configurable": {"thread_id": "1000"}},
                ledger=LlmLedger(),
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


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_the_node_never_retries_an_interrupt_or_an_exit(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, ledger: LlmLedger
) -> None:
    """Not `Exception`s: they leave the loop at once, as they left the former policy's
    `retry_on`."""
    tries: list[Attempt] = []

    async def fake_attempt(
        _state: object, _runtime: object, _config: object, attempt: Attempt, _ledger: object
    ):
        tries.append(attempt)
        raise exc

    monkeypatch.setattr(node, "llm_attempt", fake_attempt)
    with pytest.raises(type(exc)):
        asyncio.run(
            node.llm_node(
                cast(Any, object()), _runtime(), {"configurable": {"thread_id": "1"}}, ledger=ledger
            )
        )
    assert len(tries) == 1


def _failing_node_run(
    monkeypatch: pytest.MonkeyPatch, thread: str, model: str, ledger: LlmLedger
) -> tuple[int, list[float]]:
    """Run `llm_node` for one agent over an always-failing try; returns the number of tries and
    the sleeps."""
    sleeps: list[float] = []
    tries: list[Attempt] = []

    async def fake_attempt(_s: object, _r: object, _c: object, attempt: Attempt, _ledger: object):
        tries.append(attempt)
        raise ConnectionError("net")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(node, "llm_attempt", fake_attempt)
    monkeypatch.setattr(node.asyncio, "sleep", fake_sleep)
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve({"llm_model": model}),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    with pytest.raises(ConnectionError):
        asyncio.run(node.llm_node(cast(Any, object()), Runtime(context=ctx), config, ledger=ledger))
    return len(tries), sleeps


def test_two_agents_in_one_process_get_their_own_schedules(
    monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    """The point of retrying in the node: the one loop serves each agent's model and id — the
    try cap follows the model and the first wait follows the agent's phase."""
    monkeypatch.setattr("base.lm.registry.resolve_setting", _caps({"model-a": 2, "model-b": 4}))
    initial = settings.lm.llm_retry_initial_interval_seconds
    tries_a, sleeps_a = _failing_node_run(monkeypatch, "1100", "model-a", ledger)
    tries_b, sleeps_b = _failing_node_run(monkeypatch, "1200", "model-b", ledger)
    assert (tries_a, tries_b) == (2, 4)
    for first, agent_id in ((sleeps_a[0], 1100), (sleeps_b[0], 1200)):
        start = initial + retry_phase_jitter(agent_id)
        assert start <= first < start + 1.0
    assert sleeps_a[0] != sleeps_b[0]
