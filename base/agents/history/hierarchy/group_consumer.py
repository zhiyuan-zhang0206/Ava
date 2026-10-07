"""Upper-level grouping, run by the consumer loop right after a leaf is written.

A leaf landing can make level 1 due for a check; a check that closes groups
writes level-2 nodes, which can make level 2 due, and so on up. `run_group_checks`
walks up from level 1 and stops at the first level that closed nothing.

A level is checked when its open nodes (no parent) number at least `check_threshold(level)` more
than at its previous check: `AVA_UNDERSTANDING_GROUP_CHECK_OPEN` (60) for level 1, divided by
`AVA_UNDERSTANDING_GROUP_CHECK_DECAY` (3) once per level above, never below `MIN_CHECK_OPEN` (6):
60, 20, 7, 6, ... The previous
count being durable (`understanding_group_state`), so one count is never checked
twice; when groups close, the count that remains open becomes the new baseline.
A check is one conversation (`group.generate_groups`); the model is
`AVA_UNDERSTANDING_GROUP_MODEL`, else the agent's own. A failed check (provider
error, a reply still refused after the corrections) is the gap the design
accepts: the `understanding_group_failed` event, the open count recorded as
checked, and the level is looked at again after that many more nodes. Never raises into
the leaf's outcome.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from base import telemetry
from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.group import (
    GROUP_ENGINE_VERSION,
    Group,
    GroupCall,
    OpenNode,
    generate_groups,
)
from base.agents.history.hierarchy.group_store import (
    claim_check,
    load_last_checked,
    load_open_nodes,
    release_check,
    write_group_calls,
    write_groups,
)
from base.agents.observation.snapshot import agent_model_target
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.host.env.agent_slices import ModelOverrides
from base.log import logger

# A safety bound on the climb; a tree this tall is far past what history produces.
MAX_LEVEL = 12


async def run_blocking[**P, T](
    executor: Executor | None, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a blocking call off the loop: on `executor` when given, else the default one.

    The calls are pure model I/O and read no context variables, so none is carried over.
    """
    if executor is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(executor, lambda: fn(*args, **kwargs))


class _Models(Protocol):
    def get(self, model: str, overrides: ModelOverrides, reasoning: str = "") -> Any: ...


def _group_model(db: Database, agent_id: int) -> tuple[str, ModelOverrides]:
    """The configured grouping model, else the agent's own with its overrides."""
    configured = settings.agent.understanding_group_model
    if configured:
        return configured, ModelOverrides.from_pins(None)
    return agent_model_target(db, agent_id, fallback=settings.lm.hierarchy_model)


MIN_CHECK_OPEN = 6


def check_threshold(level: int) -> int:
    """New open nodes that make `level` due: the base cadence divided by the decay once per level
    above the first (rounded), never below `MIN_CHECK_OPEN` (60, 20, 7, 6, ... by default).

    A higher level collects far fewer nodes in the same time, so with one threshold for every level
    the top would trail the history by days.
    """
    base = settings.agent.understanding_group_check_open
    decay = settings.agent.understanding_group_check_decay
    return max(MIN_CHECK_OPEN, round(base / decay ** (level - 1)))


def _generate(
    models: _Models,
    model: str,
    overrides: ModelOverrides,
    level: int,
    nodes: list[OpenNode],
    calls: list[GroupCall],
) -> list[Group]:
    """Blocking: one grouping conversation; every provider call is appended to `calls`."""
    return generate_groups(
        models.get(model, overrides, settings.agent.understanding_group_reasoning),
        nodes,
        model=model,
        corrections=settings.agent.understanding_group_corrections,
        clock=Clock.from_settings(),
        # The brake on an open set that keeps growing: past three checks' worth of this level, close one.
        must_close=len(nodes) >= 3 * check_threshold(level),
        on_call=calls.append,
    )


async def _check_level(
    pool: AsyncConnectionPool,
    db: Database,
    models: _Models,
    agent_id: int,
    level: int,
    executor: Executor | None,
) -> bool:
    """Check one level if it is due; whether it closed at least one group."""
    nodes = await load_open_nodes(pool, agent_id, level)
    last = min(await load_last_checked(pool, agent_id, level), len(nodes))
    if len(nodes) < last + check_threshold(level):
        return False
    if not await claim_check(pool, agent_id, level):
        return False
    check_key = uuid.uuid4().hex
    calls: list[GroupCall] = []
    settled = False
    try:
        # Re-read under the lease: another runner may have grouped since the first read.
        nodes = await load_open_nodes(pool, agent_id, level)
        if len(nodes) < min(
            await load_last_checked(pool, agent_id, level), len(nodes)
        ) + check_threshold(level):
            return False
        model, overrides = await asyncio.to_thread(_group_model, db, agent_id)
        try:
            groups = await run_blocking(
                executor, _generate, models, model, overrides, level, nodes, calls
            )
        except GenerateError as exc:
            telemetry.emit(
                "telemetry",
                "understanding_group_failed",
                attributes={
                    "agent_id": agent_id,
                    "level": level,
                    "open_nodes": len(nodes),
                    "error": str(exc),
                },
            )
            groups = []
        finally:
            await write_group_calls(pool, agent_id, level, check_key, [n.id for n in nodes], calls)
        if groups:
            await write_groups(pool, agent_id, level, nodes, groups, model=model)
            settled = True
            return True
        await release_check(pool, agent_id, level, last_checked_open=len(nodes))
        settled = True
        return False
    finally:
        if not settled:
            await release_check(pool, agent_id, level, last_checked_open=None)


async def run_group_checks(
    pool: AsyncConnectionPool,
    db: Database,
    models: _Models,
    agent_id: int,
    *,
    executor: Executor | None = None,
) -> None:
    """Walk up the agent's levels from 1, checking each that is due; never raises.

    Checks of different agents run side by side; one `(agent, level)` is checked by one holder
    of its lease at a time. `executor` carries the blocking model call (default: the loop's).
    """
    try:
        level = 1
        while level <= MAX_LEVEL and await _check_level(
            pool, db, models, agent_id, level, executor
        ):
            level += 1
    except Exception:
        logger.opt(exception=True).warning(
            "understanding grouping ({engine}) failed for agent {agent}",
            engine=GROUP_ENGINE_VERSION,
            agent=agent_id,
        )
