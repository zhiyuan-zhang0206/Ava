"""Per-agent cached runtime state for the hosted agent runner."""

from __future__ import annotations

import hashlib
import json
import time
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel

__all__ = [
    "HostStats",
    "_AgentRuntime",
    "_StoredConfig",
    "_active_turn_config_fingerprint",
    "_config_fingerprint",
    "_copy_active_turn_context",
]

_active_turn_config_fingerprint: ContextVar[str | None] = ContextVar(
    "agent_host_active_turn_config_fingerprint", default=None
)


def _copy_active_turn_context() -> Context:
    """Copy the caller's context with no inherited config fingerprint."""
    context = copy_context()
    context.run(_active_turn_config_fingerprint.set, None)
    return context


def _config_fingerprint(
    config_overlay: dict[str, object] | None, birth_config: dict[str, object] | None
) -> str:
    """A stable digest of the two stored config maps — the runtime cache key.

    `sort_keys` makes it independent of JSONB key order, so re-reading the same
    unchanged row never looks like a change. `default=str` keeps a value psycopg
    decoded into something non-JSON (a Decimal, a datetime) from raising here: a
    fingerprint that cannot be computed would be a hard failure on the turn path,
    while a coerced one at worst compares two odd values by their repr — and
    these maps hold validated Settings scalars.
    """
    blob = json.dumps(
        {"overlay": config_overlay, "birth": birth_config}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class _StoredConfig:
    """One agent's row as the host needs it: where it lives, whether it may run,
    and the two config maps that decide what running means."""

    machine: str
    status: str
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None

    @property
    def fingerprint(self) -> str:
        return _config_fingerprint(self.config_overlay, self.birth_config)


@dataclass
class _AgentRuntime:
    """The per-agent half of a turn, cached across turns.

    `fingerprint` is what makes the cache honest: it records the config this
    runtime was built FROM, so a turn whose freshly-read config disagrees
    rebuilds instead of running the old model.
    """

    fingerprint: str
    llm: BaseChatModel
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class HostStats:
    """Counters the /healthz payload exposes — the cheap half of the cold-build
    observability, whose expensive half is the `host_agent_prepared` event.

    `wakes_skipped` is the one to read on a multi-runner cluster: the dispatcher
    pattern is cluster-wide, so every runner sees every agent's wake and each
    skips the ones it does not own.

    `config_rejected` counts wakes whose bound model configuration cannot build.
    """

    cache_hits: int = 0
    cache_misses: int = 0
    turns_started: int = 0
    wakes_skipped: int = 0
    config_rejected: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "turns_started": self.turns_started,
            "wakes_skipped": self.wakes_skipped,
            "config_rejected": self.config_rejected,
        }
