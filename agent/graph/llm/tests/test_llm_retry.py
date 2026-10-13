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
from langchain_core.exceptions import ModelConnectionError
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.hooks.compact import CompactionFailedError
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
    LLMStreamCorruptedError,
    LLMStreamStallTimeoutError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
)
from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm import registry
from base.packages.plugin_providers import build_model_catalog

_MODEL = "deepseek-flash"
_ZERO_PHASE = 1000  # agent_id % 1000 == 0: the transient schedule starts unshifted


def _wait(exc: Exception, attempts: int = 1, agent_id: int = _ZERO_PHASE) -> float | None:
    return retry_wait(
        exc,
        attempts,
        model=_MODEL,
        agent_id=agent_id,
        catalog=build_model_catalog(),
        max_attempts_pin=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ).read("lm", "llm_retry_max_attempts"),
        read_lm=lambda field: getattr(settings.lm, field),
    )


def _uniform_low(low: float, _high: float) -> float:
    return low


def _uniform_high(_low: float, high: float) -> float:
    return high


def _caps(by_model: dict[str, int]) -> Callable[..., int]:
    """A `resolve_setting` stand-in giving each model its own try cap."""

    def resolve(_name: str, *, model: str, models: object, explicit: object) -> int:
        del models, explicit
        return by_model[model]

    return resolve


def _fixed_cap(cap: int) -> Callable[..., int]:
    """A `resolve_setting` stand-in giving every model the same try cap."""

    def resolve(_name: str, *, model: str, models: object, explicit: object) -> int:
        del model, models, explicit
        return cap

    return resolve


@pytest.fixture
def ledger() -> LlmLedger:
    """A fresh successful silent-idle budget per test."""
    return LlmLedger()


def test_fatal_and_compaction_failures_are_never_retried() -> None:
    for exc in (
        FatalLLMStreamError("cap exhausted"),
        FatalProviderError("out of balance"),
        CompactionFailedError("summary retry budget exhausted"),
    ):
        assert _wait(exc) is None, type(exc).__name__


def test_other_failures_are_retried() -> None:
    assert _wait(ModelConnectionError("network")) is not None
    assert _wait(LLMStreamStallTimeoutError("stall")) is None


def test_transient_waits_double_from_the_initial_interval_plus_up_to_a_second_of_jitter() -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for attempts in (1, 2, 3):
        wait = _wait(ModelConnectionError("x"), attempts)
        assert wait is not None
        base = min(initial * 2 ** (attempts - 1), settings.lm.llm_retry_max_interval_seconds)
        assert base <= wait < base + 1.0, attempts


def test_the_model_caps_the_number_of_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_resolve(_name: str, *, model: str, models: object, explicit: object) -> int:
        seen.append(model)
        return 3

    monkeypatch.setattr(registry, "resolve_setting", fake_resolve)
    exc = ModelConnectionError("x")
    assert (
        retry_wait(
            exc,
            2,
            model="model-for-this-agent",
            agent_id=1,
            catalog=build_model_catalog(),
            max_attempts_pin=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ).read("lm", "llm_retry_max_attempts"),
            read_lm=lambda field: getattr(settings.lm, field),
        )
        is not None
    )
    assert (
        retry_wait(
            exc,
            3,
            model="model-for-this-agent",
            agent_id=1,
            catalog=build_model_catalog(),
            max_attempts_pin=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ).read("lm", "llm_retry_max_attempts"),
            read_lm=lambda field: getattr(settings.lm, field),
        )
        is None
    )
    assert set(seen) == {"model-for-this-agent"}


def test_a_spent_total_budget_ends_the_retries() -> None:
    from base.config.domains.lm import LmSettings

    assert LmSettings().llm_retry_max_total_seconds == 420.0
    exc = ModelConnectionError("budget exhausted")
    setattr(exc, RETRY_REMAINING_ATTR, 0.0)
    assert _wait(exc) is None


def test_the_clipped_wait_is_exactly_the_clipped_base_plus_the_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The former policy's `max_interval == 0.5` with jitter on, at 1.5s left: the base wait is
    0.5s and the jitter (0..1s) rides on top."""
    exc = ModelConnectionError("retryable")
    setattr(exc, RETRY_REMAINING_ATTR, 1.5)
    monkeypatch.setattr(_retry.random, "uniform", _uniform_low)
    assert _wait(exc) == 0.5
    monkeypatch.setattr(_retry.random, "uniform", _uniform_high)
    assert _wait(exc) == 1.5


def test_the_last_wait_is_clipped_to_the_remaining_budget() -> None:
    """With 1.5s left one second is reserved for the jitter the wait carries, so the base wait is
    0.5s and the whole sleep stays inside the budget; at or under a second left there is no
    jitter and the wait is what remains."""
    exc = ModelConnectionError("retryable")
    setattr(exc, RETRY_REMAINING_ATTR, 1.5)
    wait = _wait(exc)
    assert wait is not None
    assert 0.5 <= wait < 1.5

    tight = ModelConnectionError("retryable")
    setattr(tight, RETRY_REMAINING_ATTR, 0.8)
    assert _wait(tight) == 0.8


# --- retry de-phasing (task #960) ---


def test_the_phase_offset_is_stable_per_agent_and_differs_between_agents() -> None:
    assert retry_phase_jitter(11) == retry_phase_jitter(11)
    assert retry_phase_jitter(11) != retry_phase_jitter(22)
    for agent_id in (1, 7, 999, 1000, 123456):
        assert 0.0 <= retry_phase_jitter(agent_id) < RETRY_JITTER_SPAN_S


def test_the_phase_offsets_of_two_named_agents() -> None:
    """The former policy's pair: agent 1234 and 5678 get stable, different offsets in the span."""
    first, second = retry_phase_jitter(1234), retry_phase_jitter(5678)
    assert first == RETRY_JITTER_SPAN_S * 234 / 1000 and second == RETRY_JITTER_SPAN_S * 678 / 1000
    assert retry_phase_jitter(1000) == 0.0  # no offset: the schedule is exactly the configured one
    initial = settings.lm.llm_retry_initial_interval_seconds
    wait = _wait(ModelConnectionError("x"), agent_id=1000)
    assert wait is not None and initial <= wait < initial + 1.0


def test_the_transient_schedule_starts_at_the_agents_phase() -> None:
    initial = settings.lm.llm_retry_initial_interval_seconds
    for agent_id in (11, 22):
        wait = _wait(ModelConnectionError("x"), agent_id=agent_id)
        assert wait is not None
        start = initial + retry_phase_jitter(agent_id)
        assert start <= wait < start + 1.0


def _runtime(database_gate: ProcessDbGate) -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def _drive(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[object], database_gate: ProcessDbGate
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

    async def run(database_gate: ProcessDbGate) -> None:
        try:
            result["ok"] = await node.llm_node(
                cast(Any, object()),
                _runtime(database_gate=database_gate),
                {"configurable": {"thread_id": "1000"}},
                ledger=LlmLedger(),
            )
        except Exception as exc:
            result["error"] = exc

    asyncio.run(run(database_gate=database_gate))
    return tries, sleeps, result


def test_the_node_retries_a_transient_failure_and_returns_the_next_success(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    tries, sleeps, result = _drive(
        monkeypatch,
        [ModelConnectionError("a"), ModelConnectionError("b"), "ok"],
        database_gate=database_gate,
    )

    assert result == {"ok": "ok"}
    assert [t.number for t in tries] == [1, 2, 3]
    assert len({t.first_started_at for t in tries}) == 1  # one clock for the whole node
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # the second wait doubles


def test_the_node_does_not_retry_a_fatal_failure(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    tries, sleeps, result = _drive(
        monkeypatch, [FatalProviderError("balance"), "never"], database_gate=database_gate
    )

    assert isinstance(result["error"], FatalProviderError)
    assert len(tries) == 1 and sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        LLMStreamStallTimeoutError("stall"),
        TypeError("programming error"),
        ValueError("invalid input"),
        ConnectionError("another service"),
        TimeoutError("another operation"),
        LLMStreamCorruptedError("missing terminal field"),
        LLMStreamUnexpectedStopReasonError("refused", stop_reason="unknown", output_tokens=3),
        LLMStreamTruncatedError("truncated", stop_reason="length", output_tokens=3),
    ],
)
def test_the_node_does_not_repeat_unknown_or_protocol_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, database_gate: ProcessDbGate
) -> None:
    tries, sleeps, result = _drive(
        monkeypatch, [error, "must not execute"], database_gate=database_gate
    )
    assert result["error"] is error
    assert len(tries) == 1 and sleeps == []


def test_the_node_gives_up_at_the_models_attempt_cap(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    monkeypatch.setattr(_retry, "_NEVER_RETRIED", ())
    monkeypatch.setattr(registry, "resolve_setting", _fixed_cap(3))
    tries, sleeps, result = _drive(
        monkeypatch, [ModelConnectionError(str(i)) for i in range(5)], database_gate=database_gate
    )

    assert str(result["error"]) == "2"  # the third failed try ends the node
    assert len(tries) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_the_node_never_retries_an_interrupt_or_an_exit(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
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
                cast(Any, object()),
                _runtime(database_gate=database_gate),
                {"configurable": {"thread_id": "1"}},
                ledger=ledger,
            )
        )
    assert len(tries) == 1


def _failing_node_run(
    monkeypatch: pytest.MonkeyPatch,
    thread: str,
    model: str,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
) -> tuple[int, list[float]]:
    """Run `llm_node` for one agent over an always-failing try; returns the number of tries and
    the sleeps."""
    sleeps: list[float] = []
    tries: list[Attempt] = []

    async def fake_attempt(_s: object, _r: object, _c: object, attempt: Attempt, _ledger: object):
        tries.append(attempt)
        raise ModelConnectionError("net")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(node, "llm_attempt", fake_attempt)
    monkeypatch.setattr(node.asyncio, "sleep", fake_sleep)
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            {"llm_model": model},
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        ),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    with pytest.raises(ModelConnectionError):
        asyncio.run(node.llm_node(cast(Any, object()), Runtime(context=ctx), config, ledger=ledger))
    return len(tries), sleeps


def test_two_agents_in_one_process_get_their_own_schedules(
    monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger, database_gate: ProcessDbGate
) -> None:
    """The point of retrying in the node: the one loop serves each agent's model and id — the
    try cap follows the model and the first wait follows the agent's phase."""
    monkeypatch.setattr(registry, "resolve_setting", _caps({"model-a": 2, "model-b": 4}))
    initial = settings.lm.llm_retry_initial_interval_seconds
    tries_a, sleeps_a = _failing_node_run(
        monkeypatch, "1100", "model-a", ledger, database_gate=database_gate
    )
    tries_b, sleeps_b = _failing_node_run(
        monkeypatch, "1200", "model-b", ledger, database_gate=database_gate
    )
    assert (tries_a, tries_b) == (2, 4)
    for first, agent_id in ((sleeps_a[0], 1100), (sleeps_b[0], 1200)):
        start = initial + retry_phase_jitter(agent_id)
        assert start <= first < start + 1.0
    assert sleeps_a[0] != sleeps_b[0]
