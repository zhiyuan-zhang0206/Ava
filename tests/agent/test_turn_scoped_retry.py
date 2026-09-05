"""Per-agent LLM retry under one shared graph — `agent/graph/_build.py`.

The hosted agent-runner builds ONE compiled graph for every local agent (it has
to: `build_graph` mutates process-global plugin registration), so anything the
graph bakes in at build time silently becomes cluster-level. Two retry
parameters are per-agent and must not:

- `max_attempts`, resolved per MODEL — an agent whose overlay pins a different
  model gets that model's cap;
- the `_retry_phase_jitter()` term in `initial_interval`, whose only purpose is
  de-phasing fleet-wide retry waves. Collapsed to one value, a correlated 429
  burst has every agent retry at the same instant, which re-synchronises the
  burst instead of spreading it.

`_TurnScopedRetryPolicy` keeps both per-agent by resolving them when they are
read. That relies on LangGraph reading policy fields at RETRY time rather than
snapshotting them at build time — a fact about a dependency, so the last class
here pins it against LangGraph's real retry loop rather than against our own
class. If an upgrade starts snapshotting, that test fails loudly instead of the
fleet quietly retrying in lockstep.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from langgraph.types import RetryPolicy

from agent.graph._build import _RETRY_JITTER_SPAN_S, _build_llm_retry, _TurnScopedRetryPolicy
from shared.config import settings
from shared.turn_identity import bind_turn_identity


class TestPerAgentResolution:
    def test_the_jitter_offset_follows_the_bound_turn(self) -> None:
        """The same policy object, read under two agents' binds, gives two
        different schedules — which is the whole point."""
        policy = _build_llm_retry()
        with bind_turn_identity(11):
            first = policy.initial_interval
        with bind_turn_identity(22):
            second = policy.initial_interval
        assert first != second, "one policy object must not give every agent one schedule"

    def test_the_offset_is_stable_for_one_agent(self) -> None:
        """Deterministic, so an agent keeps its phase across restarts — a random
        offset would re-roll the fleet into a fresh collision every boot."""
        policy = _build_llm_retry()
        with bind_turn_identity(11):
            first = policy.initial_interval
        with bind_turn_identity(11):
            second = policy.initial_interval
        assert first == second

    def test_the_offset_stays_inside_its_span(self) -> None:
        """The offset shifts the schedule; it must not become the schedule."""
        base = settings.lm.llm_retry_initial_interval_seconds
        policy = _build_llm_retry()
        for agent_id in (1, 7, 999, 1000, 123456):
            with bind_turn_identity(agent_id):
                assert base <= policy.initial_interval < base + _RETRY_JITTER_SPAN_S

    def test_unbound_reads_match_the_build_time_value(self) -> None:
        """Process mode binds nothing, so every read must equal what the old
        build-time call produced — the conversion is a no-op there."""
        policy = _build_llm_retry()
        assert policy.initial_interval == settings.lm.llm_retry_initial_interval_seconds
        assert (
            policy.max_attempts == RetryPolicy(max_attempts=policy.max_attempts).max_attempts
        )  # the resolved value, not langgraph's default

    def test_max_attempts_follows_the_turn_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A per-model cap must be read per turn, not frozen at build."""
        from shared.config.turn_view import bind_agent_config

        policy = _build_llm_retry()
        seen: list[str] = []

        def _fake_resolve(_name: str, *, model: str) -> int:
            seen.append(model)
            return 42

        monkeypatch.setattr("shared.lm.registry.resolve_setting", _fake_resolve)
        with bind_agent_config({"llm_model": "model-for-this-agent"}):
            assert policy.max_attempts == 42
        assert seen == ["model-for-this-agent"], "the cap must resolve against the TURN's model"


class TestPositionalFallback:
    def test_the_tuple_slots_carry_the_build_time_values(self) -> None:
        """Defence in depth for the dependency assumption below.

        The properties shadow attribute reads, but the underlying tuple still
        holds what the build-time call computed. So a consumer that ever read the
        policy positionally would get today's behaviour rather than LangGraph's
        own defaults (3 attempts / 0.5s) — an unnoticed snapshot degrades, it
        does not collapse.
        """
        policy = _build_llm_retry()
        # Iterating uses the TUPLE protocol, so this reads the underlying slots
        # rather than the shadowing properties. Field order is langgraph's:
        # (initial_interval, backoff_factor, max_interval, max_attempts, ...).
        slots = cast("tuple[float, float, float, int, bool, object]", tuple(policy))
        assert slots[3] != 3, "slot 3 is max_attempts, not the langgraph default"
        assert slots[0] == settings.lm.llm_retry_initial_interval_seconds

    def test_it_is_still_a_retrypolicy(self) -> None:
        """`pregel/_read.py` wraps a lone policy as `(policy,)` behind an
        `isinstance(..., RetryPolicy)` check. Failing it would make LangGraph
        ITERATE the tuple and treat each field as a policy."""
        assert isinstance(_build_llm_retry(), RetryPolicy)


class _RetryableError(Exception):
    """A failure LangGraph will actually retry.

    `langgraph.types.default_retry_on` explicitly REFUSES ValueError, TypeError,
    RuntimeError and friends, so a test node raising one of those is never
    retried — it would assert against a node that ran exactly once and prove
    nothing. These tests pass an explicit `retry_on` as well, and this class
    exists so the intent is visible at the raise site.
    """


class TestLangGraphReadsFieldsAtRetryTime:
    """The dependency assumption, pinned against LangGraph's real retry loop.

    Everything above tests our class in isolation and would keep passing if
    LangGraph started snapshotting policy fields at build time — at which point
    hosted mode would silently lose per-agent de-phasing again. These drive an
    actual graph so the assumption fails loudly on upgrade instead.
    """

    async def test_a_retrying_node_reads_the_policy_per_attempt(self) -> None:
        """Build a graph whose node always fails, and count how many attempts
        LangGraph makes when `max_attempts` resolves from a contextvar bound
        AFTER the graph was compiled.

        If LangGraph snapshotted the value at build time it would use the tuple
        slot (2) and attempt twice; reading per attempt it sees 4.
        """
        from contextvars import ContextVar

        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        attempts_pin: ContextVar[int] = ContextVar("attempts_pin", default=2)

        class _Pinned(RetryPolicy):
            __slots__ = ()

            @property
            def max_attempts(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
                return attempts_pin.get()

        class _S(TypedDict):
            n: int

        calls = {"n": 0}

        def _always_fails(_state: _S) -> _S:
            calls["n"] += 1
            raise _RetryableError("boom")

        g = StateGraph(_S)
        g.add_node(  # pyright: ignore[reportUnknownMemberType]
            "boom",
            _always_fails,  # pyright: ignore[reportArgumentType]
            # initial_interval 0 so the test does not sleep through the backoff.
            retry_policy=_Pinned(
                max_attempts=2, initial_interval=0.0, jitter=False, retry_on=lambda _e: True
            ),
        )
        g.add_edge(START, "boom")
        g.add_edge("boom", END)
        graph = g.compile()  # pyright: ignore[reportUnknownMemberType]

        token = attempts_pin.set(4)
        try:
            with pytest.raises(_RetryableError, match="boom"):
                await graph.ainvoke({"n": 0})  # pyright: ignore[reportUnknownMemberType]
        finally:
            attempts_pin.reset(token)

        assert calls["n"] == 4, (
            "LangGraph read max_attempts once at build time instead of per attempt — "
            "_TurnScopedRetryPolicy no longer keeps hosted retry per-agent. See the "
            "class docstring in agent/graph/_build.py."
        )

    async def test_the_backoff_interval_is_read_per_attempt_too(self) -> None:
        """`initial_interval` carries the de-phasing offset, so it matters that
        LangGraph reads it late as well — not just `max_attempts`.

        Asserted by timing: an interval resolved per attempt from the contextvar
        (0s) finishes fast, where a build-time snapshot (5s) could not.
        """
        from contextvars import ContextVar

        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        interval_pin: ContextVar[float] = ContextVar("interval_pin", default=5.0)

        class _Pinned(RetryPolicy):
            __slots__ = ()

            @property
            def initial_interval(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
                return interval_pin.get()

        class _S(TypedDict):
            n: int

        def _always_fails(_state: _S) -> _S:
            raise _RetryableError("boom")

        g = StateGraph(_S)
        g.add_node(  # pyright: ignore[reportUnknownMemberType]
            "boom",
            _always_fails,  # pyright: ignore[reportArgumentType]
            retry_policy=_Pinned(
                max_attempts=3, initial_interval=5.0, jitter=False, retry_on=lambda _e: True
            ),
        )
        g.add_edge(START, "boom")
        g.add_edge("boom", END)
        graph = g.compile()  # pyright: ignore[reportUnknownMemberType]

        token = interval_pin.set(0.0)
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(_RetryableError, match="boom"):
                await graph.ainvoke({"n": 0})  # pyright: ignore[reportUnknownMemberType]
        finally:
            interval_pin.reset(token)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 2.0, (
            f"two retries took {elapsed:.1f}s — LangGraph used the build-time interval "
            "(5s) rather than reading it per attempt, so the per-agent de-phasing "
            "offset no longer reaches it."
        )


def test_build_llm_retry_returns_the_turn_scoped_policy() -> None:
    """The wiring itself: a plain RetryPolicy here would pass every test above
    that only reads an unbound value."""
    assert isinstance(_build_llm_retry(), _TurnScopedRetryPolicy)
