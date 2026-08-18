"""Test that FatalLLMStreamError is excluded from the LLM retry policy.

If the retry policy mistakenly retries FatalLLMStreamError, the consecutive-error
cap is useless -- the agent would keep retrying after the cap fires.
"""

from __future__ import annotations

import pytest

from agent.graph._build import _build_llm_retry
from agent.graph._llm import (
    FatalLLMStreamError,
    FatalProviderError,
    LLMStreamStallTimeoutError,
)


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
    from agent.graph._build import (
        _RETRY_JITTER_SPAN_S,
        _build_llm_retry,
        _retry_phase_jitter,
    )
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
