"""Per-provider outbound LLM concurrency caps.

The default `AVA_LLM_MAX_CONCURRENT=deepseek:31` reserves 31 calls for each
of the default 16 active turns (496 of DeepSeek Pro's 500 account slots). The
cap applies per provider *around the whole SDK call* — SDK-internal retries
included — so a burst of agents or a batch `ava.understand` / `ava.web.fetch`
job cannot push a provider past its account concurrency ceiling. An explicit
empty value disables all caps; operators changing the active-turn budget must
adjust the configured cap to preserve the account-wide allocation.

Two acquire flavors, both pass-through when the provider is unconfigured:

- `sync(provider)` — a `threading.BoundedSemaphore` for the synchronous
  invoke paths (`shared/lm/_call.py` `invoke_text`); safe across worker
  threads (ava.understand's thread pool).
- `async_acquire(provider)` — an `asyncio.Semaphore` for the streaming
  agent path (`agent/graph/_llm.py` `_stream_with_cache_retry`); created
  lazily on first use so no event-loop binding happens at import time.

Provider keys are the model-prefix keys of `shared/lm/factory.py` with any
trailing dash stripped (`deepseek` / `claude` / `gpt` / `gemini` / `mimo` /
`kimi` / `glm` / `qwen`); the config rejects unknown keys at parse time
(fail fast). `None` as a provider key is always a pass-through, so call
sites that cannot resolve a provider degrade to unlimited rather than
crash.

Design notes (from the 2026-08-05 429 audit, task #786): industry practice
for an account-level concurrency ceiling is client-side concurrency control
(primary) + exponential-backoff retry (secondary) — the limiter is the
primary layer; `invoke_text`'s backoff stays the secondary layer.

Caveat: a slot held by one LLM call is not
re-entrant — an agent streaming under a held slot whose `execute_code` then
calls `ava.understand` on the same provider queues behind itself (deadlock
at limit=1). Batch and understand paths must share the budget consciously (or
run on a provider key with headroom).
"""

import asyncio
import threading
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache


@lru_cache(maxsize=1)
def _provider_keys() -> frozenset[str]:
    from shared.lm.factory import provider_key_map

    return frozenset(prefix.rstrip("-") for prefix in provider_key_map())


def known_provider_keys() -> frozenset[str]:
    """The provider keys `AVA_LLM_MAX_CONCURRENT` accepts — DERIVED from
    `shared/lm/factory.py:provider_key_map` (plugin prefixes, with a trailing
    dash stripped when present), the provider registry's single source
    (audit 2026-08-08 P2: the old explicit list drifted in exactly the one
    direction a config parse cannot catch — factory gained a provider and the
    limiter silently passed it through). Lazy import + lru_cache:
    `_concurrency` is a leaf on hot paths and must not pull the model catalog
    at module scope. The cache is invalidated on every provider-plugin
    registration (provider_api.register_invalidator), so a plugin provider's
    key becomes a legal `AVA_LLM_MAX_CONCURRENT` key without a second
    mechanism.
    """
    return _provider_keys()


def _invalidate_known_provider_keys_cache() -> None:
    """Clear the lru_cache behind `known_provider_keys` — called by
    provider_api after every plugin registration (registered via
    register_invalidator)."""
    _provider_keys.cache_clear()


# Register the invalidator eagerly: `_concurrency` is imported by hot call
# paths, and provider_api must not import it back (layering + cycle).
from shared.lm import provider_api  # noqa: E402

provider_api.REGISTRY.register_invalidator(_invalidate_known_provider_keys_cache)


# A provider Retry-After longer than this is treated as a misconfiguration
# (an LLM provider should not ask for multi-minute waits); same spirit as the
# provider SDKs' own 60s cap, slightly wider since our layer is the last one.
_MAX_RETRY_AFTER_RESPECT_S = 120.0


def parse_limits(csv: str) -> dict[str, int]:
    """Parse `AVA_LLM_MAX_CONCURRENT` (`provider:limit,provider:limit`).

    Raises ValueError on any malformed segment or unknown provider key —
    fail fast at first use rather than silently dropping a cap the operator
    believed was active. Keys are case-insensitive; limits must be positive
    integers.
    """
    csv = (csv or "").strip()
    if not csv:
        return {}
    limits: dict[str, int] = {}
    for raw_segment in csv.split(","):
        segment = raw_segment.strip()
        if not segment:
            raise ValueError(
                f"AVA_LLM_MAX_CONCURRENT={csv!r}: empty segment — expected "
                "'provider:limit,provider:limit'"
            )
        key, sep, raw_limit = segment.partition(":")
        key = key.strip().lower()
        if not sep or not key:
            raise ValueError(
                f"AVA_LLM_MAX_CONCURRENT={csv!r}: segment {segment!r} has no 'provider:limit' form"
            )
        if key not in known_provider_keys():
            raise ValueError(
                f"AVA_LLM_MAX_CONCURRENT={csv!r}: unknown provider {key!r} — "
                f"known keys: {', '.join(sorted(known_provider_keys()))}"
            )
        try:
            limit = int(raw_limit.strip())
        except ValueError:
            raise ValueError(
                f"AVA_LLM_MAX_CONCURRENT={csv!r}: limit for {key!r} "
                f"({raw_limit.strip()!r}) is not an integer"
            ) from None
        if limit <= 0:
            raise ValueError(
                f"AVA_LLM_MAX_CONCURRENT={csv!r}: limit for {key!r} must be a positive integer"
            )
        limits[key] = limit
    return limits


class LLMConcurrencyLimiter:
    """Provider-keyed concurrency caps for outbound LLM requests.

    Construct with `from_csv` (the settings surface) or `from_mapping` (tests).
    Unconfigured providers and `None` keys pass through untouched.
    """

    def __init__(self, limits: Mapping[str, int] | None = None) -> None:
        self._limits: dict[str, int] = dict(limits or {})
        self._sync_sems: dict[str, threading.BoundedSemaphore] = {}
        self._async_sems: dict[str, asyncio.Semaphore] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_csv(cls, csv: str) -> "LLMConcurrencyLimiter":
        return cls(parse_limits(csv))

    @classmethod
    def from_mapping(cls, limits: Mapping[str, int] | None = None) -> "LLMConcurrencyLimiter":
        return cls(limits)

    @property
    def limits(self) -> dict[str, int]:
        return dict(self._limits)

    @property
    def is_enabled(self) -> bool:
        return bool(self._limits)

    def _normalize(self, provider: str | None) -> str | None:
        if provider is None:
            return None
        key = provider.strip().lower()
        return key if key in self._limits else None

    def _sync_semaphore(self, key: str) -> threading.BoundedSemaphore:
        sem = self._sync_sems.get(key)
        if sem is None:
            with self._lock:
                sem = self._sync_sems.get(key)
                if sem is None:
                    sem = threading.BoundedSemaphore(self._limits[key])
                    self._sync_sems[key] = sem
        return sem

    def _async_semaphore(self, key: str) -> asyncio.Semaphore:
        sem = self._async_sems.get(key)
        if sem is None:
            with self._lock:
                sem = self._async_sems.get(key)
                if sem is None:
                    sem = asyncio.Semaphore(self._limits[key])
                    self._async_sems[key] = sem
        return sem

    @contextmanager
    def sync(self, provider: str | None) -> Generator[None, None, None]:
        """Hold the provider's sync cap for one SDK call (no-op when unconfigured)."""
        key = self._normalize(provider)
        if key is None:
            yield
            return
        sem = self._sync_semaphore(key)
        acquired = sem.acquire(timeout=None)  # wait: queue, don't 429 the provider
        if not acquired:  # pragma: no cover — timeout=None always acquires
            yield
            return
        try:
            yield
        finally:
            sem.release()

    @asynccontextmanager
    async def async_acquire(self, provider: str | None) -> AsyncGenerator[None, None]:
        """Hold the provider's async cap around one streaming call (no-op when unconfigured)."""
        key = self._normalize(provider)
        if key is None:
            yield
            return
        sem = self._async_semaphore(key)
        async with sem:
            yield


_limiter: LLMConcurrencyLimiter | None = None


def get_limiter() -> LLMConcurrencyLimiter:
    """The process-wide limiter (built once from `AVA_LLM_MAX_CONCURRENT`).

    A single shared instance is required for the caps to mean anything —
    per-call instances would each admit their own `limit` concurrent calls.
    The config is read at first LLM request and thereafter; like every
    `restart_required: agent` setting, a change takes effect on the next
    agent restart.
    """
    global _limiter  # noqa: PLW0603 — module-level singleton (shared/log.py pattern)
    if _limiter is None:
        from shared.config import settings

        _limiter = LLMConcurrencyLimiter.from_csv(settings.lm.llm_max_concurrent)
    return _limiter


def _reset_limiter() -> None:
    """Drop the cached singleton — tests only (a fresh process reads config anew)."""
    global _limiter  # noqa: PLW0603 — test seam for a process-lifetime global
    _limiter = None


__all__ = [
    "LLMConcurrencyLimiter",
    "get_limiter",
    "known_provider_keys",
    "parse_limits",
]
