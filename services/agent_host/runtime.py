"""Per-agent cached runtime state for the hosted agent runner, plus the
wake-time admission of an agent's stored model configuration."""

from __future__ import annotations

import hashlib
import json
import time
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from shared.config import settings
from shared.lm.factory import validate_model_config
from shared.lm.registry import resolve_available_model
from shared.log import logger

__all__ = [
    "HostStats",
    "_AgentRuntime",
    "_StoredConfig",
    "_active_turn_config_fingerprint",
    "_config_fingerprint",
    "_copy_active_turn_context",
    "admit_stored_model",
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
    `config_normalized` counts wakes whose stored `llm_model` pin named a
    withdrawn model and was bound as its registered fallback instead.
    """

    cache_hits: int = 0
    cache_misses: int = 0
    turns_started: int = 0
    wakes_skipped: int = 0
    config_rejected: int = 0
    config_normalized: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "turns_started": self.turns_started,
            "wakes_skipped": self.wakes_skipped,
            "config_rejected": self.config_rejected,
            "config_normalized": self.config_normalized,
        }


def admit_stored_model(
    pins: dict[str, Any],
    *,
    agent_id: int,
    stored: _StoredConfig,
    stats: HostStats,
    rejected: dict[int, str],
    normalized: dict[int, str],
) -> bool:
    """Admit the stored model configuration a hosted wake is about to bind.

    Fail fast before ANY turn work — the status flip included: an overlay naming
    a model the registry does not know would otherwise explode inside
    `build_chat_model` on every wake (the dispatcher drops the task, the pending
    scan re-wakes the still-pending inbound, and the host loops on crash
    tracebacks — incident #2344). The effective model resolves exactly as the
    turn view does (overlay > birth > cluster default). A wake whose
    configuration cannot build is consumed without raising: the durable inbound
    stays pending, and a fixed overlay is served on the next scan.

    A pin naming a model the registry has since withdrawn is not refused: it is
    rewritten in place to its registered fallback, the same resolution
    `build_chat_model` would otherwise apply only at the final build. Binding
    the resolved id here means the turn view reads it, the usage attribution
    records it, and the exec children re-emitted from these pins inherit it —
    instead of a withdrawn pin leaking to every reader until the last build.

    Both outcomes are noted once per stored config state (fingerprint) in
    `rejected` / `normalized` and counted in `stats`; neither writes to the
    database. `pins` is mutated in place — the caller binds it after this
    returns True.

    Returns True when the wake may proceed, False when the configuration cannot
    build (the caller returns without a turn).
    """
    model = pins.get("llm_model") or settings.lm.llm_model
    try:
        validate_model_config(model=model)
    except ValueError as exc:
        stats.config_rejected += 1
        if rejected.get(agent_id) != stored.fingerprint:
            rejected[agent_id] = stored.fingerprint
            logger.error(
                "hosted wake for agent {agent_id} rejected before turn — "
                "its model config cannot build: {reason}. Fix the agent's "
                "llm_model (restart(config_overlay=...) or the spawn "
                "overlay) and the next wake serves normally.",
                event="host_config_rejected",
                agent_id=agent_id,
                reason=str(exc),
            )
        return False
    rejected.pop(agent_id, None)
    if "llm_model" in pins:
        pinned_model = pins["llm_model"]
        resolved_model = resolve_available_model(pinned_model)
        if resolved_model != pinned_model:
            pins["llm_model"] = resolved_model
            stats.config_normalized += 1
            if normalized.get(agent_id) != stored.fingerprint:
                normalized[agent_id] = stored.fingerprint
                logger.warning(
                    "hosted wake for agent {agent_id} normalized its stored "
                    "llm_model {requested} -> {resolved} (withdrawn model); "
                    "the turn binds the registered fallback",
                    event="host_config_normalized",
                    agent_id=agent_id,
                    requested=pinned_model,
                    resolved=resolved_model,
                )
    return True


class TurnOutcome:
    """How one hosted invocation ended, as the settle boundary needs it.

    `crashed` stamps the corpse marker: fatal LLM classes + unclassified
    exceptions — every path where the turn died without completing work. A
    no-work park (crash-fresh halted claim, open circuit breaker) ends
    `turn_idle` with `crashed` False, and the settle deliberately leaves the
    marker untouched, so the park cannot relabel a corpse healthy.

    `aborted` marks the fatal-abort settlement: the turn's failure was written
    into the flushed checkpoint (`settle_turn_failure`) before this outcome was
    returned, so the checkpoint is durable and the settle boundary may
    reconcile the abort's claimed inbounds. Unclassified exceptions end
    `crashed` without it — their checkpoint was never settled.

    `truncated` marks the update straggler-reap end (task #4016): the drain
    CAS-marked the row 'restarting' mid-turn, so the turn's fail-closed guard
    read refused and `services.agent_host.truncation` classified that mark as
    the deliberate truncation it is. `crashed` stays False — no corpse marker
    is stamped — and the settle boundary skips the abort reconcile: the
    successor boundary that settles the reap mark owns the claimed rows.
    """

    __slots__ = ("aborted", "crashed", "exited", "truncated")

    def __init__(
        self, *, exited: bool, crashed: bool, aborted: bool = False, truncated: bool = False
    ) -> None:
        self.exited = exited
        self.crashed = crashed
        self.aborted = aborted
        self.truncated = truncated
