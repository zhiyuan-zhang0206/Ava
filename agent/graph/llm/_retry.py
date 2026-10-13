"""Bounded retries of trusted transient provider errors within one LLM node.

Stalls, protocol failures, permanent rejections and unknown exceptions end
this turn. The node never retries a graph or code execution.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.graph.hooks.compact import CompactionFailedError
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
)
from base.lm.catalog import ModelCatalog
from base.lm.errors import is_retryable_provider_error

RETRY_JITTER_SPAN_S = 10.0
RETRY_REMAINING_ATTR = "_ava_retry_budget_remaining_seconds"
# The backoff factor the transient schedule was tuned with (every incident validated base 2).
_BACKOFF_FACTOR = 2.0
_NEVER_RETRIED = (FatalLLMStreamError, FatalProviderError, CompactionFailedError)


@dataclass(frozen=True)
class Attempt:
    """One try of the node: its 1-based number and when the node's first try started."""

    number: int
    first_started_at: float

    def elapsed_seconds(self) -> float:
        """Wall-clock seconds since the first try started (the retry waits included)."""
        return max(0.0, time.time() - self.first_started_at)


def retry_phase_jitter(agent_id: int) -> float:
    """The agent's stable offset in [0, `RETRY_JITTER_SPAN_S`) of the transient schedule."""
    return RETRY_JITTER_SPAN_S * (agent_id % 1000) / 1000.0


def _retryable_failure(exc: Exception) -> bool:
    if isinstance(exc, _NEVER_RETRIED):
        return False
    return is_retryable_provider_error(exc)


def retry_wait(
    exc: Exception,
    attempts: int,
    *,
    model: str,
    agent_id: int,
    catalog: ModelCatalog,
    max_attempts_pin: int | None,
    read_lm: Callable[[str], Any],
) -> float | None:
    """Seconds to sleep before the next try after the `attempts`-th failed one; None ends the node.

    `attempts` counts failed tries (1 after the first failure).
    """
    if not _retryable_failure(exc):
        return None
    from base.lm.registry import resolve_setting

    max_attempts = resolve_setting(
        "llm_retry_max_attempts",
        model=model,
        models=catalog.models,
        explicit=max_attempts_pin,
    )
    remaining = getattr(exc, RETRY_REMAINING_ATTR, None)
    remaining = remaining if isinstance(remaining, float) else None
    if remaining is not None and remaining <= 0.0:
        return None
    if attempts >= max_attempts:
        return None
    max_interval = read_lm("llm_retry_max_interval_seconds")
    if remaining is not None:
        # Reserve the up-to-one-second jitter so a sleep cannot drift past the node's budget.
        max_interval = min(max_interval, remaining - (1.0 if remaining > 1.0 else 0.0))
    initial = read_lm("llm_retry_initial_interval_seconds") + retry_phase_jitter(agent_id)
    interval = min(max_interval, initial * (_BACKOFF_FACTOR ** (attempts - 1)))
    if remaining is None or remaining > 1.0:
        return interval + random.uniform(0, 1)  # noqa: S311 — retry jitter, not security
    return interval
