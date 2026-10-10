"""Per-agent cached runtime state for the hosted agent runner, plus the
wake-time admission of an agent's stored model configuration."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agent.process_boot import boot_agent_scope
from base.host.env.agent_slices import AgentBrain, AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.factory import validate_model_config
from base.lm.registry import resolve_available_model
from base.log import logger

__all__ = [
    "HostCachePolicy",
    "HostPolicy",
    "HostStats",
    "_AgentRuntime",
    "_StoredConfig",
    "_config_fingerprint",
    "admit_stored_model",
]


@dataclass(frozen=True)
class HostCachePolicy:
    """The cache limits read at one eviction, never at host construction."""

    idle_ttl_seconds: float
    size: int


@dataclass(frozen=True)
class HostPolicy:
    """Capacity is fixed at construction; cache and model readers remain live."""

    max_concurrent_turns: int
    cache: Callable[[], HostCachePolicy]
    default_model: Callable[[], str]
    llm_override: Callable[[], str]

    def resolve_slices(
        self,
        pins: Mapping[str, Any],
        plugin_pins: Mapping[str, Mapping[str, Any]],
        plugin_configs: Mapping[str, BaseModel] | None,
    ) -> AgentSlices:
        """Resolve the model at the existing slice boundary, with pins taking precedence."""
        brain = AgentBrain(pins["llm_model"] if "llm_model" in pins else self.default_model())
        return AgentSlices.resolve(pins, plugin_pins, plugin_configs=plugin_configs, brain=brain)


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
    catalog: ModelCatalog,
    llm_override: str,
    default_model: Callable[[], str],
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
    model = pins.get("llm_model") or default_model()
    try:
        validate_model_config(model=model, catalog=catalog, llm_override=llm_override)
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
        resolved_model = resolve_available_model(pinned_model, models=catalog.models)
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

    `native_held` skips inbound repair when an accepted native cancel lacks
    checkpoint/resource proof. It never labels that proof gap a healthy abort.

    `truncated` marks a deliberate external end of the turn, classified bound
    to the turn's own incarnation: an applied force terminate (task #4180; the
    watchdog wedge recovery / a CLI force / a machine pause, whose command
    awaits its observation by this pump's boundary). `crashed` stays False —
    no corpse marker is stamped — and the settle boundary skips the abort
    reconcile: the successor boundary that observes the force owns the claimed
    rows.
    """

    __slots__ = ("aborted", "crashed", "exited", "native_held", "truncated")

    def __init__(
        self,
        *,
        exited: bool,
        crashed: bool,
        aborted: bool = False,
        truncated: bool = False,
        native_held: bool = False,
    ) -> None:
        self.exited = exited
        self.crashed = crashed
        self.aborted = aborted
        self.truncated = truncated
        self.native_held = native_held


async def cached_runtime(
    runtimes: OrderedDict[int, _AgentRuntime],
    stats: HostStats,
    agent_id: int,
    fingerprint: str,
    slices: AgentSlices,
    build: Callable[[int, str, AgentSlices], Awaitable[_AgentRuntime]],
    evict: Callable[[], None],
) -> _AgentRuntime:
    """Build and retain the existing cache entry; the host owns its lifetime."""
    cached = runtimes.get(agent_id)
    if cached is not None and cached.fingerprint == fingerprint:
        cached.last_used = time.monotonic()
        runtimes.move_to_end(agent_id)
        stats.cache_hits += 1
        return cached

    reason = "cold" if cached is None else "config_changed"
    stats.cache_misses += 1
    started = time.monotonic()
    runtime = await build(agent_id, fingerprint, slices)
    runtimes[agent_id] = runtime
    runtimes.move_to_end(agent_id)
    logger.info(
        "hosted runtime for agent {agent_id} built ({reason})",
        event="host_agent_prepared",
        agent_id=agent_id,
        reason=reason,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    evict()
    return runtime


def evict_runtimes(
    runtimes: OrderedDict[int, _AgentRuntime],
    in_flight: set[int],
    *,
    policy: HostCachePolicy,
    now: float,
) -> None:
    """Evict idle runtimes by age and least-recent use; misses rebuild on wake.

    Active runtimes survive both bounds, including turns longer than the
    idle TTL. Completion refreshes idle time and LRU position before eviction.
    Active-agent admission can exceed the cache budget; completion returns
    the warm cache to its configured size as active runtimes settle.
    """
    cutoff = now - policy.idle_ttl_seconds
    aged = [a for a, r in runtimes.items() if r.last_used < cutoff and a not in in_flight]
    for agent_id in aged:
        del runtimes[agent_id]
    cap = policy.size
    for agent_id in list(runtimes):
        if len(runtimes) <= cap:
            break
        if agent_id not in in_flight:
            del runtimes[agent_id]


async def read_last_active_at(pool: AsyncConnectionPool, agent_id: int) -> datetime | None:
    """This agent's real activity clock — `agents_meta.last_active_at`.

    Handed to `TurnScheduler` so an uncancellable-turn report can say how
    long the agent has actually been silent. Deliberately THIS column and not
    the `/api/agents` field of the same name: that one is
    `MAX(inbound_messages.created_at)` (`base/agents/observation/snapshot.py`) and goes
    stale during exactly the long turns where "is it wedged?" is a real
    question — issue #183. This column is written on every completed LLM step
    (`agent/graph/llm/node.py:_persist_last_active`).

    Returns None when the row is gone; raising is left to the caller's
    best-effort wrapper, which runs on the shutdown path.
    """
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT last_active_at FROM agents_meta WHERE id = %s", (agent_id,))
        ).fetchone()
    return None if row is None else row[0]


async def build_runtime(
    agent_id: int,
    fingerprint: str,
    slices: AgentSlices,
    *,
    catalog: ModelCatalog,
    llm_override: str,
) -> _AgentRuntime:
    """Build the retained model after the host repaired its original admission."""
    llm, _binding = await boot_agent_scope(
        agent_id,
        slices.brain.llm_model,
        slices.overrides,
        catalog=catalog,
        llm_override=llm_override,
    )
    return _AgentRuntime(fingerprint=fingerprint, llm=llm)


def refresh_cached_runtime(
    runtimes: OrderedDict[int, _AgentRuntime],
    in_flight: set[int],
    agent_id: int,
    evict: Callable[[], None],
) -> None:
    """Refresh the retained model's recency after the owned turn has finished."""
    cached = runtimes.get(agent_id)
    if cached is not None:
        cached.last_used = time.monotonic()
        runtimes.move_to_end(agent_id)
    in_flight.discard(agent_id)
    evict()
