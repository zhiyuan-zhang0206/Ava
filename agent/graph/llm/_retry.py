"""The llm node's own retry loop: which failures are retried and how long each retry waits.

The agent host builds ONE graph for every local agent, so a graph-level retry policy cannot carry
a per-agent schedule: LangGraph reads its policy fields at retry time with no handle on whose turn
it is. The node therefore retries itself. `retry_wait` decides, from the failure, the number of
failed tries so far, the agent's model and the agent's id, how long to sleep before the next try —
or that the failure ends the node.

The schedule is the one the policy used to have:

- trusted transient provider failures and owned stream stalls: `max_attempts` tries (a
  per-model cap, an explicit `AVA_LLM_RETRY_MAX_ATTEMPTS` / overlay wins), waits of
  `llm_retry_initial_interval_seconds` doubling up to `llm_retry_max_interval_seconds`, plus up to
  one second of random jitter; the node attaches its remaining wall-clock retry budget to the
  exception (`RETRY_REMAINING_ATTR`) and the wait is clipped to it, no retry once it is spent;
- stall pairs (`LLMStreamStallPairError`: the streaming segment stalled and the non-streaming
  fallback then timed out) run on a separate delayed schedule — minutes, not seconds: the waits
  start at `llm_stall_retry_initial_interval_seconds`, double, cap at
  `llm_stall_retry_max_interval_seconds`, jittered ±`llm_stall_retry_jitter_fraction`, for at most
  `llm_stall_retry_max_consecutive` consecutive pairs (a spent streak ends the turn at the node's
  entry check, so refusing here is the backstop);
- unknown errors, protocol validation failures, fatal errors and failed compaction are never retried.

Retry-wave de-phasing: a correlated failure hits every agent at the same moment and an identical
schedule would retry in lockstep, each wave re-synchronizing the burst. The transient schedule
starts at a stable per-agent offset in [0, `RETRY_JITTER_SPAN_S`) derived from the agent id, so
waves stay de-phased across the fleet and an agent keeps its phase across restarts.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LlmLedger,
    LLMStreamStallPairError,
    LLMStreamStallTimeoutError,
)
from agent.hooks.compact import CompactionFailedError
from base.host.net.resilience import jittered
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


def delayed_stall_sleep(streak: int, *, read_lm: Callable[[str], Any]) -> float:
    """The jittered wait before the retry after stall pair number `streak`.

    Exponential with a cap: ``initial x 2**(streak-1)``, capped at
    ``llm_stall_retry_max_interval_seconds``, then multiplicative
    ±``llm_stall_retry_jitter_fraction`` jitter (the 2026-09-14/15 wave hit 36 agents on 3 machines
    within 10s, so an unjittered schedule would re-synchronize the fleet). Random per grant, not
    the per-agent phase: with only a few grants each must land in a fresh spot.
    """
    base = min(
        read_lm("llm_stall_retry_initial_interval_seconds") * (2 ** (streak - 1)),
        read_lm("llm_stall_retry_max_interval_seconds"),
    )
    return jittered(base, span=base * read_lm("llm_stall_retry_jitter_fraction"), mode="random")


def _retryable_failure(exc: Exception) -> bool:
    if isinstance(exc, _NEVER_RETRIED):
        return False
    return isinstance(exc, LLMStreamStallTimeoutError) or is_retryable_provider_error(exc)


def retry_wait(
    exc: Exception,
    attempts: int,
    *,
    model: str,
    agent_id: int,
    ledger: LlmLedger,
    catalog: ModelCatalog,
    max_attempts_pin: int | None,
    read_lm: Callable[[str], Any],
) -> float | None:
    """Seconds to sleep before the next try after the `attempts`-th failed one; None ends the node.

    `attempts` counts failed tries (1 after the first failure); `ledger` holds the stall-pair streak.
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
    max_pairs = read_lm("llm_stall_retry_max_consecutive")
    if isinstance(exc, LLMStreamStallPairError) and max_pairs > 0:
        thread = str(agent_id)
        streak = ledger.stall_pair_streak(thread) + 1
        if streak > max_pairs:
            ledger.reset_stall_pair_streak(thread)
            return None
        ledger.record_stall_pair_streak(thread, streak)
        # Headroom: the shared attempts gate must not pre-empt the streak cap when transient
        # failures earlier in the same sequence consumed part of the transient count.
        if attempts >= max_attempts + max_pairs:
            return None
        return delayed_stall_sleep(streak, read_lm=read_lm)

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
