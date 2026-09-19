"""Test that FatalLLMStreamError is excluded from the LLM retry policy.

If the retry policy mistakenly retries FatalLLMStreamError, the consecutive-error
cap is useless -- the agent would keep retrying after the cap fires.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent.graph._build import _build_llm_retry
from agent.graph._llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LLMStreamStallPairError,
    LLMStreamStallPairExhaustedError,
    LLMStreamStallTimeoutError,
)
from shared.config import settings
from shared.turn_identity import bind_turn_identity


def test_retry_policy_excludes_fatal_llm_stream_error() -> None:
    """FatalLLMStreamError must NOT be retried; other LLMStreamErrors must be."""
    policy = _build_llm_retry()
    retry_on = policy.retry_on
    assert callable(retry_on), "retry_on must be callable (we set a lambda)"

    # FatalLLMStreamError: must NOT retry
    assert not retry_on(FatalLLMStreamError("test cap exhausted")), (  # type: ignore[arg-type]
        "FatalLLMStreamError must be excluded from retry -- "
        "otherwise the consecutive-error cap is useless"
    )

    # LLMStreamStallTimeoutError (normal stream error): must retry
    assert retry_on(LLMStreamStallTimeoutError("test stall")), (  # type: ignore[arg-type]
        "LLMStreamStallTimeoutError must be retried -- only FatalLLMStreamError should be excluded"
    )

    # KeyboardInterrupt / SystemExit: must NOT retry (preserve default behavior)
    assert not retry_on(KeyboardInterrupt()), (  # type: ignore[arg-type]
        "KeyboardInterrupt must still be excluded from retry (default behavior)"
    )
    assert not retry_on(SystemExit()), (  # type: ignore[arg-type]
        "SystemExit must still be excluded from retry (default behavior)"
    )

    # FatalProviderError (permanent 402/401/403 rejection): must NOT retry
    assert not retry_on(FatalProviderError("test out of balance")), (  # type: ignore[arg-type]
        "FatalProviderError must be excluded from retry -- a permanent billing/auth "
        "rejection cannot flip on retry"
    )

    # Generic network error: must retry
    assert retry_on(ConnectionError("test network")), (  # type: ignore[arg-type]
        "Generic network errors must still be retried"
    )


def test_retry_policy_default_on_is_callable() -> None:
    """The default retry_on was a function; our override must also be callable."""
    policy = _build_llm_retry()
    assert callable(policy.retry_on), "retry_on must be callable (no regression to tuple mode)"


def test_retry_policy_stops_when_its_total_time_budget_is_exhausted() -> None:
    """A retry failure after the wall-clock budget must end the retry loop."""
    from agent.graph._build import _RETRY_REMAINING_ATTR
    from shared.config.lm import LmSettings

    assert LmSettings().llm_retry_max_total_seconds == 420.0
    exc = ConnectionError("budget exhausted")
    setattr(exc, _RETRY_REMAINING_ATTR, 0.0)

    assert not _build_llm_retry().retry_on(exc)  # type: ignore[arg-type]


def test_retry_policy_clips_the_next_wait_to_the_remaining_total_budget() -> None:
    """The final retry sleep must not consume more than the node has left."""
    from agent.graph._build import _RETRY_REMAINING_ATTR

    policy = _build_llm_retry()
    exc = ConnectionError("retryable")
    setattr(exc, _RETRY_REMAINING_ATTR, 1.5)

    assert policy.retry_on(exc)  # type: ignore[arg-type]
    assert policy.max_interval == 0.5  # reserves LangGraph's at-most-one-second jitter
    assert policy.jitter is True  # consumes the per-attempt handoff


# --- LLM retry fleet de-phasing (task #960) ---


def test_retry_policy_jitter_enabled() -> None:
    """LangGraph's per-attempt jitter is explicitly locked on (default True,
    but the fleet de-phasing intent is load-bearing — a future langgraph
    default flip must not silently re-synchronize the fleet's retries)."""
    policy = _build_llm_retry()
    assert policy.jitter is True


def test_retry_policy_phase_jitter_zero_without_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AVA_AGENT_ID (tests / non-agent entry points) → no offset: the
    schedule stays exactly the configured 30→60→120→240→480."""
    from agent.graph._build import _retry_phase_jitter
    from shared.config import settings

    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert _retry_phase_jitter() == 0.0
    assert _build_llm_retry().initial_interval == settings.lm.llm_retry_initial_interval_seconds


def test_retry_policy_phase_jitter_deterministic_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_AGENT_ID set → the whole schedule is offset by a stable per-agent
    amount in [0, span), so a correlated failure (429 burst / provider drift)
    cannot re-sync the fleet's retry waves into lockstep (heartbeat-daemon
    de-phasing pattern)."""
    from agent.graph._build import _RETRY_JITTER_SPAN_S, _build_llm_retry, _retry_phase_jitter
    from shared.config import settings

    monkeypatch.setenv("AVA_AGENT_ID", "1234")
    j1 = _retry_phase_jitter()
    assert j1 == _retry_phase_jitter()  # deterministic: stable across restarts
    assert 0.0 <= j1 < _RETRY_JITTER_SPAN_S
    assert (
        _build_llm_retry().initial_interval == settings.lm.llm_retry_initial_interval_seconds + j1
    )

    monkeypatch.setenv("AVA_AGENT_ID", "5678")
    j2 = _retry_phase_jitter()
    assert j2 != j1  # different agents retry on different phases


# --- Delayed stall schedule (task #3884) ---
#
# A stall pair (stream segment + non-streaming fallback both stalled in one
# call) runs on its own schedule: initial `llm_stall_retry_initial_interval`
# (5min), doubling, capped, jittered, at most `llm_stall_retry_max_consecutive`
# consecutive pairs — minutes-scale waits so a degraded provider is not
# hot-looped, spread by jitter so the fleet does not re-synchronize.


@pytest.fixture
def bound_thread() -> Iterator[str]:
    """Bind a turn identity so the streak keys on it, and clean it up after."""
    from agent.graph._llm_errors import _reset_stall_pair_streak

    with bind_turn_identity(6363):
        yield "6363"
    _reset_stall_pair_streak("6363")


def _read_policy_fields_in_langgraph_order(policy: object) -> tuple[int, float, float, float, bool]:
    """Read the policy fields the way LangGraph's retry loop does.

    The synchronous handoff (thread-local) is consumed by the `jitter` read,
    the last one in the sequence — reading in a different order sees different
    state.
    """
    from langgraph.types import RetryPolicy

    assert isinstance(policy, RetryPolicy)
    return (
        policy.max_attempts,
        policy.initial_interval,
        policy.max_interval,
        policy.backoff_factor,
        policy.jitter,
    )


def test_stall_pair_grants_the_delayed_schedule(bound_thread: str) -> None:
    """A pair error is retried under the delayed schedule: minutes-scale
    jittered wait, no compounding backoff, its own attempts headroom."""
    from agent.graph._llm_errors import _stall_pair_streak
    from shared.config.turn_view import turn_settings
    from shared.lm.registry import resolve_setting

    policy = _build_llm_retry()
    exc = LLMStreamStallPairError("pair", stage="ttft")

    assert policy.retry_on(exc)  # type: ignore[arg-type]
    max_attempts, sleep, max_interval, backoff, jitter = _read_policy_fields_in_langgraph_order(
        policy
    )

    base_attempts = resolve_setting("llm_retry_max_attempts", model=turn_settings.lm.llm_model)
    assert max_attempts == base_attempts + settings.lm.llm_stall_retry_max_consecutive
    assert 300.0 * 0.75 <= sleep <= 300.0 * 1.25  # initial 5min, jittered +-25%
    assert max_interval == sleep  # the computed wait, not the transient cap
    assert backoff == 1.0  # never compounded again
    assert jitter is False  # the wait already carries its multiplicative jitter
    assert _stall_pair_streak(bound_thread) == 1
    # Handoff consumed: later reads fall back to the transient values.
    assert policy.backoff_factor == 2.0


def test_stall_pair_wait_doubles_and_caps(bound_thread: str) -> None:
    """Waits follow initial x 2**(streak-1) capped at the max interval —
    5, 10, 20, 30 minutes — each in the configured jitter band."""
    policy = _build_llm_retry()
    bands = {1: 300.0, 2: 600.0, 3: 1200.0, 4: 1800.0}
    for streak, base in bands.items():
        exc = LLMStreamStallPairError("pair")
        assert policy.retry_on(exc)  # type: ignore[arg-type]
        sleep = policy.initial_interval
        assert base * 0.75 <= sleep <= base * 1.25, f"streak {streak}"
        assert policy.max_attempts > 0  # read in LangGraph order, consumes alongside
        assert policy.backoff_factor == 1.0
        assert policy.jitter is False


def test_stall_pair_refuses_past_the_cap_and_resets(bound_thread: str) -> None:
    """Past `llm_stall_retry_max_consecutive` the retry is refused and the
    streak resets (the next turn starts with a fresh budget)."""
    from agent.graph._llm_errors import _record_stall_pair_streak, _stall_pair_streak

    policy = _build_llm_retry()
    _record_stall_pair_streak(bound_thread, settings.lm.llm_stall_retry_max_consecutive)
    assert not policy.retry_on(LLMStreamStallPairError("pair"))  # type: ignore[arg-type]
    assert _stall_pair_streak(bound_thread) == 0


def test_stall_pair_entry_cap_raises_fatal_and_resets(bound_thread: str) -> None:
    """A spent streak fails the turn at node entry as a FatalLLMStreamError
    (the established alive-and-idle settlement), and resets."""
    from agent.graph._llm_errors import (
        _check_stall_pair_cap,
        _record_stall_pair_streak,
        _stall_pair_streak,
    )

    _record_stall_pair_streak(bound_thread, settings.lm.llm_stall_retry_max_consecutive)
    with pytest.raises(LLMStreamStallPairExhaustedError):
        _check_stall_pair_cap(bound_thread)
    assert _stall_pair_streak(bound_thread) == 0
    assert isinstance(LLMStreamStallPairExhaustedError("x"), FatalLLMStreamError)
    assert isinstance(LLMStreamStallPairError("x"), LLMStreamStallTimeoutError)


def test_stall_pair_errors_skip_the_consecutive_tracker(bound_thread: str) -> None:
    """The tracker's cap (3) must not pre-empt the delayed schedule's 4th
    grant, so pair errors never enter it; plain stalls still do."""
    from agent.graph._llm_errors import (
        _clear_consecutive_errors,
        _consecutive_errors,
        _record_consecutive_error,
    )

    _clear_consecutive_errors(bound_thread)
    _record_consecutive_error(bound_thread, LLMStreamStallPairError("pair"))
    assert bound_thread not in _consecutive_errors
    _record_consecutive_error(bound_thread, LLMStreamStallTimeoutError("stall"))
    assert _consecutive_errors[bound_thread] == ("LLMStreamStallTimeoutError", 1)
    _clear_consecutive_errors(bound_thread)


def test_delayed_schedule_disabled_falls_back_to_transient(
    bound_thread: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`llm_stall_retry_max_consecutive=0` disables the delayed regime: pair
    errors retry (or stop) exactly like any transient error, with no streak."""
    from agent.graph._build import _RETRY_JITTER_SPAN_S, _RETRY_REMAINING_ATTR
    from agent.graph._llm_errors import _stall_pair_streak

    monkeypatch.setattr(settings.lm, "llm_stall_retry_max_consecutive", 0)
    policy = _build_llm_retry()
    exc = LLMStreamStallPairError("pair")

    assert policy.retry_on(exc)  # type: ignore[arg-type]
    assert (
        settings.lm.llm_retry_initial_interval_seconds
        <= policy.initial_interval
        < settings.lm.llm_retry_initial_interval_seconds + _RETRY_JITTER_SPAN_S
    )
    assert policy.jitter is True
    assert _stall_pair_streak(bound_thread) == 0

    setattr(exc, _RETRY_REMAINING_ATTR, 0.0)
    assert not policy.retry_on(exc)  # type: ignore[arg-type]
