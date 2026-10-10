"""The consumer loop for chunk-triggered understanding, run by the agent host.

Producers (the llm node and the compact paths, `agent/hooks/understanding_chunks.py`)
enqueue chunks into `understanding_chunk_jobs`; this loop claims them
(`FOR UPDATE SKIP LOCKED`, so every runner's loop shares one queue) as soon as they are due, with no
cap on how many run at once — one agent's jobs still run strictly in order (the claim only hands
out an agent's oldest live job, so the jobs in flight never outnumber the agents with work),
different agents' side by side; rate limiting is the provider's 429 and the shared retry
backoff of `invoke_response` —
reads the chunk's segment from the checkpoint, asks the agent's own model to
divide it into groups of message units and summarize each, and writes one depth-1
`understanding_nodes` row per group. The request is the
agent's conversation up to the chunk's end plus a trailing instruction on the
plain path (SystemMessage in-band, `execute_code` bound, the model built with
the agent's own effort), so the provider serves the prefix from cache.

No staleness judgment: a claimed chunk is always described. A chunk whose
checkpoint has not caught up (the writer persists every Nth super-step) goes
back to the queue and is retried after a spacing; indices that drifted fail the
job loudly; a grouping reply still refused after the corrections is a
generation failure (retried, then failed). Each poll samples the queue's depth, with this
runner's in-flight jobs, as the `understanding_backlog` event. A job that ends
`done` is followed, in the same task, by the upper-level grouping checks of its agent
(`group_consumer.py`). A manual build's upper-level rebuild (`rebuild.py`) is claimed by the same
loop once its agent has no chunk job left.

The loop never raises — a raise would cancel the whole host's task group — and neither does a
job: each runs in a task of the loop's own `TaskGroup`, which catches everything, so one job's
failure touches no other.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from base import telemetry
from base.agents.history.checkpoint import (
    CheckpointReadError,
    FullHistory,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_history_full,
)
from base.agents.history.hierarchy.chunk_generate import ChunkResult, generate_chunk
from base.agents.history.hierarchy.chunks import (
    GIVE_UP_AFTER_SECONDS,
    MAX_ATTEMPTS,
    ChunkCall,
    ChunkDriftError,
    ChunkEmptyError,
    ChunkJob,
    ChunkNotReadyError,
    GroupNode,
    LocatedChunk,
    backlog,
    claim_job,
    covered_spans,
    finish_job,
    locate_chunk,
    message_time,
    release_job,
    slice_chunk,
    uncovered,
    write_chunk_calls,
    write_group_nodes,
)
from base.agents.history.hierarchy.generate import GenerateError, GenParams, build_generation_llm
from base.agents.history.hierarchy.group_consumer import run_blocking, run_group_checks
from base.agents.history.hierarchy.rebuild import (
    MAX_REBUILD_ATTEMPTS,
    RebuildJob,
    claim_rebuild,
    finish_rebuild,
    rebuild_pending,
    release_rebuild,
    run_rebuild,
)
from base.agents.history.hierarchy.units import divide_units
from base.agents.observation.snapshot import agent_model_target
from base.config import settings
from base.db import Database
from base.deploy.maintenance import admission
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.log import logger

POLL_SECONDS = 2.0

# Generation failures (a provider error after its own retries, a model that
# will not answer in text) are retried a few times, not the full checkpoint-lag
# budget: the same request failing repeatedly is not about to start working.
GENERATION_MAX_ATTEMPTS = 3

# A database error that a retry can outlive (a restart, a dropped connection, a pool wait).
_TRANSIENT = (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout)


@dataclass(frozen=True)
class Outcome:
    """How one claimed job ended: done / failed / skipped, or back to the queue: `retry` after a
    generation failure (an attempt spent), `wait` when nothing about the job itself went wrong
    (the checkpoint has not caught up, the database blinked: no attempt spent)."""

    status: str
    error: str | None = None


def _load_segments(
    db: Database, agent_id: int, boundary_id: str | None
) -> tuple[FullHistory, int | None]:
    """The stitched history and, for a closing chunk, the index of its segment."""
    history = load_checkpoint_history_full(db, agent_id)
    if boundary_id is None:
        return history, None
    ascending = list(reversed(list_compact_boundary_checkpoint_ids(db, agent_id)))
    return history, ascending.index(boundary_id)


class ModelCache:
    """One chat model per (model, overrides), kept for the consumer's life and never closed.

    Closing is wrong in a resident process: langchain-anthropic hands every
    instance one process-wide `lru_cache`d httpx client, so closing one model's
    client breaks every later sync call in the host.
    Reuse also keeps providers that build a client per instance from opening a
    new pool per job. Keys are the cluster's distinct agent model
    configurations, so the cache stays small.
    """

    def __init__(self, catalog: ModelCatalog, llm_override: str) -> None:
        self.catalog = catalog
        self.llm_override = llm_override
        self._models: dict[tuple[str, ModelOverrides, str], Any] = {}
        self._lock = threading.Lock()  # jobs build models from several worker threads

    def get(self, model: str, overrides: ModelOverrides, reasoning: str = "") -> Any:
        """The model; `reasoning` is `AVA_UNDERSTANDING_GROUP_REASONING`'s value: empty keeps
        the model's own tier, `off` disables thinking, anything else is the effort asked."""
        key = (model, overrides, reasoning)
        with self._lock:
            if key not in self._models:
                self._models[key] = build_generation_llm(
                    model,
                    GenParams(reasoning_effort=reasoning) if reasoning not in ("", "off") else None,
                    overrides=overrides,
                    catalog=self.catalog,
                    llm_override=self.llm_override,
                    thinking_off=reasoning == "off",
                )
            return self._models[key]


def _describe(
    models: ModelCache,
    model: str,
    overrides: ModelOverrides,
    located: LocatedChunk,
    tools: Sequence[Any],
    calls: list[ChunkCall],
    agent_id: int,
) -> ChunkResult:
    """Blocking: the agent's own model's groups and summaries of a located chunk.

    Every provider call's raw record is appended to `calls`, the failed one too.
    """
    return generate_chunk(
        models.get(model, overrides),
        located.prefix,
        located.start_offset,
        model=model,
        catalog=models.catalog,
        agent_id=agent_id,
        tools=tools,
        corrections=settings.agent.understanding_group_corrections,
        on_call=calls.append,
    )


def _plan_nodes(result: ChunkResult, located: LocatedChunk) -> list[GroupNode]:
    """The groups as storable nodes: stitched spans, first / last message times."""
    base = located.span[0]
    nodes: list[GroupNode] = []
    for group in result.groups:
        first, last = result.units[group.first], result.units[group.last]
        msgs = located.messages[first.i0 : last.i1 + 1]
        nodes.append(
            GroupNode(
                (base + first.i0, base + last.i1),
                message_time(msgs, last=False),
                message_time(msgs, last=True),
                group.summary,
            )
        )
    return nodes


def _report_gaps(job: ChunkJob, located: LocatedChunk, left_over: list[tuple[int, int]]) -> None:
    """The event for what this job will not describe: uncovered runs it left for another job
    (the part of a chunk that overlapped existing nodes in the middle) and the turns a closing
    chunk's boundary snapshot lacks."""
    if not left_over and located.missing is None:
        return
    gaps = [f"{a}-{b}" for a, b in left_over]
    if located.missing is not None:
        gaps.append(f"request {located.missing[0]}-{located.missing[1]} (not in the snapshot)")
    telemetry.emit(
        "telemetry",
        "understanding_chunk_gap",
        attributes={"agent_id": job.agent_id, "job_id": job.id, "gaps": ", ".join(gaps)},
    )


def _gave_up(job: ChunkJob) -> Outcome | None:
    """`failed` for a job claimed too often or waiting too long, else None."""
    if job.attempts > MAX_ATTEMPTS:
        return Outcome("failed", f"gave up after {job.attempts - 1} attempts")
    if job.age_seconds > GIVE_UP_AFTER_SECONDS:
        hours = job.age_seconds / 3600
        return Outcome("failed", f"gave up: still not describable after {hours:.0f} h")
    return None


async def _undescribed_part(
    pool: AsyncConnectionPool, job: ChunkJob, located: LocatedChunk
) -> tuple[LocatedChunk | None, list[tuple[int, int]]]:
    """`located` cut to its first run that no level-1 node covers, and the runs after it.

    None for the chunk when nothing is left. A chunk with several uncovered runs describes the
    first; the rest are returned for the caller to report (never dropped silently).
    """
    covered = await covered_spans(pool, job.agent_id, located.span)
    if not covered:
        return located, []
    gaps = uncovered(located.span, covered)
    if not gaps:
        return None, []
    return slice_chunk(located, *gaps[0]), gaps[1:]


async def _run_job(
    pool: AsyncConnectionPool,
    db: Database,
    job: ChunkJob,
    tools: Sequence[Any],
    models: ModelCache,
    executor: ThreadPoolExecutor | None = None,
) -> Outcome:
    """Describe one claimed chunk and store its nodes; the outcome says how it ended."""
    if (gave_up := _gave_up(job)) is not None:
        return gave_up
    model, overrides = await asyncio.to_thread(
        agent_model_target,
        db,
        job.agent_id,
        fallback=settings.lm.hierarchy_model,
        catalog=models.catalog,
    )
    try:
        history, closing_segment = await asyncio.to_thread(
            _load_segments, db, job.agent_id, job.boundary_checkpoint_id
        )
    except CheckpointReadError as exc:
        return Outcome("wait", f"checkpoint read failed: {exc}")
    except ValueError:
        return Outcome("failed", f"boundary checkpoint {job.boundary_checkpoint_id} is gone")
    try:
        located = locate_chunk(
            history,
            start_index=job.start_index,
            end_index=job.end_index,
            end_msg_id=job.end_msg_id,
            closing_segment=closing_segment,
        )
    except ChunkNotReadyError as exc:
        return Outcome("wait", str(exc))
    except ChunkDriftError as exc:
        # Waiting does not heal it: the indices drifted.
        return Outcome("failed", str(exc))
    except ChunkEmptyError as exc:
        return Outcome("skipped", str(exc))
    located, left_over = await _undescribed_part(pool, job, located)
    if located is None:
        return Outcome("skipped", "the chunk is already described")
    if all(unit.kind == "note" for unit in divide_units(list(located.messages))):
        # Nothing but framework-injected notes: there is no matter to describe.
        return Outcome("skipped", "the chunk holds only framework notes")
    calls: list[ChunkCall] = []
    try:
        result = await run_blocking(
            executor,
            _describe,
            models,
            model,
            overrides,
            located,
            tools,
            calls,
            job.agent_id,
        )
    except GenerateError as exc:
        if job.attempts >= GENERATION_MAX_ATTEMPTS:
            return Outcome("failed", str(exc))
        return Outcome("retry", str(exc))
    finally:
        await write_chunk_calls(pool, job, calls)
    await write_group_nodes(pool, job, _plan_nodes(result, located), model=model)
    _report_gaps(job, located, left_over)
    return Outcome("done")


async def _settle(pool: AsyncConnectionPool, job: ChunkJob, outcome: Outcome) -> None:
    """Record a job's outcome on its row, with the event for the ones that end without a node."""
    if outcome.status in ("retry", "wait"):
        await release_job(
            pool,
            job.id,
            error=outcome.error or "",
            count_attempt=outcome.status == "retry",
            waiting=outcome.status == "wait",
        )
        return
    await finish_job(pool, job.id, status=outcome.status, error=outcome.error)
    if outcome.status == "failed":
        telemetry.emit(
            "telemetry",
            "understanding_chunk_failed",
            attributes={
                "agent_id": job.agent_id,
                "job_id": job.id,
                "attempts": job.attempts,
                "error": outcome.error or "",
            },
        )
    elif outcome.status == "skipped":
        telemetry.emit(
            "telemetry",
            "understanding_chunk_skipped",
            attributes={
                "agent_id": job.agent_id,
                "job_id": job.id,
                "reason": outcome.error or "",
            },
        )


@dataclass(frozen=True)
class Replay:
    """A replay tool's consumer of ONE agent's already-enqueued jobs (`scripts/verify/understanding_replay.py`).

    Compaction segments are independent (a closing chunk seals every open group), so the
    segments' jobs run side by side, each segment's in order. The upper-level checks are left
    out: leaves of later segments land before earlier ones finish, and a grouping over a
    level with gaps would be wrong; the tool's `regroup` rebuilds them in message order.
    """

    agent_id: int


class _Consumer:
    """One runner's consumer: claims every due job, runs each in its own task.

    A job is in flight from claim to the end of its upper-level checks. Each job owns a
    one-thread pool that keeps its blocking model calls off the host's default executor and is
    shut down when the job ends. Every task is owned by the `TaskGroup` handed to `claim`, and
    `_process` catches everything, so no job outlives or breaks the loop.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        db: Database,
        tools: Sequence[Any],
        replay: Replay | None = None,
        *,
        catalog: ModelCatalog,
        llm_override: str,
    ) -> None:
        self.pool, self.db, self.tools, self.replay = pool, db, tools, replay
        self.models = ModelCache(catalog, llm_override)
        self.in_flight = 0
        self.finished = asyncio.Event()  # set whenever a job ends

    async def emit_backlog(self) -> None:
        depth = await backlog(self.pool)
        telemetry.emit(
            "telemetry",
            "understanding_backlog",
            attributes={
                "pending": depth.pending,
                "running": depth.running,
                "oldest_pending_age_seconds": depth.oldest_pending_age_seconds,
                "in_flight": self.in_flight,
            },
        )

    async def claim(self, tg: asyncio.TaskGroup) -> bool:
        """Claim one due job and start it; False when nothing is due."""
        self.in_flight += 1  # reserved before the await: this is the only claimer
        try:
            job = await claim_job(
                self.pool,
                agent_id=self.replay.agent_id if self.replay else None,
                segment_parallel=self.replay is not None,
            )
            if job is not None:
                tg.create_task(self._process(job))
                return True
            # A replay leaves the upper levels to its tool's own regroup; live consumers rebuild
            # an agent's tree once its chunk jobs have all ended (`rebuild.py`).
            rebuild = await claim_rebuild(self.pool) if self.replay is None else None
            if rebuild is not None:
                tg.create_task(self._process_rebuild(rebuild))
                return True
            self.in_flight -= 1
            return False
        except BaseException:
            self.in_flight -= 1
            raise

    async def _put_back(self, job: ChunkJob) -> None:
        """Release a job this consumer holds, without spending an attempt; never raises."""
        try:
            await asyncio.shield(
                release_job(
                    self.pool, job.id, error="host stopping", count_attempt=False, waiting=None
                )
            )
        except Exception:
            logger.opt(exception=True).warning(
                "understanding chunk {job} could not be put back; its lease will lapse",
                job=job.id,
            )

    async def _process(self, job: ChunkJob) -> None:
        """Run, settle and group-check one claimed job; never raises."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="understanding")
        try:
            try:
                outcome = await _run_job(self.pool, self.db, job, self.tools, self.models, executor)
            except asyncio.CancelledError:
                # The host is stopping (a rollout): hand the job back at once instead of making
                # the next host wait out the lease. A crash still relies on the lease.
                await self._put_back(job)
                raise
            except _TRANSIENT as exc:
                # The database blinked (a restart during a roll): the job is not at fault.
                logger.warning(
                    "understanding chunk {job} (agent {agent}) hit a database error, waiting: {exc!r}",
                    job=job.id,
                    agent=job.agent_id,
                    exc=exc,
                )
                outcome = Outcome("wait", f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "understanding chunk {job} (agent {agent}) crashed",
                    job=job.id,
                    agent=job.agent_id,
                )
                outcome = Outcome("failed", f"{type(exc).__name__}: {exc}")
            await _settle(self.pool, job, outcome)
            if (
                outcome.status == "done"
                and self.replay is None
                and not await rebuild_pending(self.pool, job.agent_id)
            ):
                # The nodes just written may make a level due for grouping (group_consumer.py);
                # a pending rebuild groups every leaf itself, so checks now would be redone.
                await run_group_checks(
                    self.pool, self.db, self.models, job.agent_id, executor=executor
                )
        except Exception:
            # A settle that failed leaves the row `running`; its lease lapses and it is retaken.
            logger.opt(exception=True).warning(
                "understanding chunk {job} (agent {agent}) could not be settled",
                job=job.id,
                agent=job.agent_id,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            self.in_flight -= 1
            self.finished.set()

    async def _process_rebuild(self, rebuild: RebuildJob) -> None:
        """Run and settle one claimed upper-level rebuild; never raises."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="understanding")
        try:
            try:
                leaves = await run_rebuild(
                    self.pool, self.db, self.models, rebuild.agent_id, executor=executor
                )
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(
                        release_rebuild(
                            self.pool, rebuild.id, error="host stopping", count_attempt=False
                        )
                    )
                except Exception:
                    logger.opt(exception=True).warning(
                        "understanding rebuild {rebuild} could not be put back; its lease will lapse",
                        rebuild=rebuild.id,
                    )
                raise
            except _TRANSIENT as exc:
                await release_rebuild(
                    self.pool, rebuild.id, error=f"{type(exc).__name__}: {exc}", count_attempt=False
                )
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "understanding rebuild {rebuild} (agent {agent}) failed",
                    rebuild=rebuild.id,
                    agent=rebuild.agent_id,
                )
                error = f"{type(exc).__name__}: {exc}"
                if rebuild.attempts >= MAX_REBUILD_ATTEMPTS:
                    await finish_rebuild(self.pool, rebuild.id, status="failed", error=error)
                    telemetry.emit(
                        "telemetry",
                        "understanding_rebuild_failed",
                        attributes={
                            "agent_id": rebuild.agent_id,
                            "rebuild_id": rebuild.id,
                            "attempts": rebuild.attempts,
                            "error": error,
                        },
                    )
                else:
                    await release_rebuild(self.pool, rebuild.id, error=error)
            else:
                await finish_rebuild(self.pool, rebuild.id, status="done", leaves=leaves)
        except Exception:
            # A settle that failed leaves the row `running`; its lease lapses and it is retaken.
            logger.opt(exception=True).warning(
                "understanding rebuild {rebuild} (agent {agent}) could not be settled",
                rebuild=rebuild.id,
                agent=rebuild.agent_id,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            self.in_flight -= 1
            self.finished.set()

    async def _live_jobs(self, agent_id: int) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM understanding_chunk_jobs"
                " WHERE agent_id = %s AND status IN ('pending', 'running')",
                (agent_id,),
            )
            row = await cur.fetchone()
        assert row is not None, "an aggregate query always returns one row"  # noqa: S101
        return int(row[0])

    async def run_until_idle(self) -> None:
        """Claim and finish jobs until nothing is due and nothing is in flight (tests, tools).

        A replay also waits out jobs sent back to the queue (their retry spacing).
        """
        async with asyncio.TaskGroup() as tg:
            while True:
                while await self.claim(tg):
                    pass
                if not self.in_flight:
                    if self.replay is None or not await self._live_jobs(self.replay.agent_id):
                        return
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                self.finished.clear()
                await self.finished.wait()

    async def run_forever(self) -> None:
        async with asyncio.TaskGroup() as tg:
            last_sample = 0.0
            while True:
                # A held unit claims nothing: the stop window is database-quiet.
                if not admission.quiesced():
                    try:
                        now = time.monotonic()
                        if now - last_sample >= POLL_SECONDS:
                            last_sample = now
                            await self.emit_backlog()
                        while await self.claim(tg):
                            pass
                    except Exception:
                        logger.opt(exception=True).warning("understanding consumer poll failed")
                self.finished.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.finished.wait(), POLL_SECONDS)


async def replay_jobs(
    pool: AsyncConnectionPool,
    db: Database,
    tools: Sequence[Any],
    agent_id: int,
    *,
    catalog: ModelCatalog,
    llm_override: str,
) -> None:
    """Describe every enqueued job of one agent, its segments in parallel (the replay tool)."""
    await _Consumer(
        pool, db, tools, Replay(agent_id), catalog=catalog, llm_override=llm_override
    ).run_until_idle()


async def understanding_loop_forever(
    pool: AsyncConnectionPool,
    db: Database,
    tools: Sequence[Any],
    *,
    catalog: ModelCatalog,
    llm_override: str,
) -> None:
    """Consume the chunk queue for the host's whole life; returns at once when the feature is off.

    `tools` is the agent's tool schema (`[execute_code]`), bound for cache parity;
    the host hands it in because the kernel's schema lives above this package.
    Every due job runs at once, one agent's never together.
    """
    if not settings.agent.understanding_enabled:
        logger.info("[agent-host] understanding consumer idle — AVA_UNDERSTANDING_ENABLED is off")
        return
    await _Consumer(pool, db, tools, catalog=catalog, llm_override=llm_override).run_forever()
