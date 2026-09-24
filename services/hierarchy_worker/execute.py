"""Job child — run one build attempt to completion and record its outcome.

Executed as `python -m services.hierarchy_worker.job --job-id N` (the
entrypoint in `job.py` calls `execute_job`). The child owns its job row's
completion: it writes the materialized nodes, records the run's scope and
token stats, and — only when the run skipped nothing — advances the agent's
scan cursor (a `compact` job) or the tail-seal delta column (a `tail` job,
task #3981 C; a tail run advances no cursor — it seals a trailing stretch,
it does not claim compact coverage). A crash, a kill, or the parent's
deadline leaves the row `running`; the parent runner recovers it, and a
retry resumes from the hash cache with zero redone nodes.

The cursor advance target is the newest boundary read *before* loading: the
load reads every boundary that exists at its own later start, so the target
is guaranteed to be inside what the run walked. A boundary stamped after the
read simply triggers the next job instead of being silently skipped —
advancing to a value the run did not seal would lose that stretch for good.

Generation is agent-shaped (task #4674): requests ride the target agent's own
conversation prefix and tool schema so the provider serves them from its
prefix cache, and the generation model is the agent's own effective model —
`shared.agent_snapshot.agent_effective_model`.
"""

from __future__ import annotations

import time
import traceback

from agent.llm import execute_code
from services.hierarchy_worker.scan import KIND_COMPACT, KIND_TAIL
from shared.agent_snapshot import agent_effective_model
from shared.agents.history.checkpoint import (
    latest_checkpoint_id,
    list_compact_boundary_checkpoint_ids,
)
from shared.agents.history.hierarchy import ENGINE_VERSION, PROMPT_VERSION
from shared.agents.history.hierarchy.generate import build_generation_llm
from shared.agents.history.hierarchy.pipeline import MaterializedTree, build_agent_tree
from shared.agents.history.hierarchy.store import load_known_texts, write_tree
from shared.config import settings
from shared.db import connect
from shared.db_transaction import write_transaction
from shared.lm.factory import close_chat_model
from shared.log import logger

# The error text kept on the job row: a diagnostic tail, never a full dump
# repeated into the DB row.
_ERROR_TAIL_CHARS = 4000


def execute_job(job_id: int) -> int:
    """Run one `running` job to completion; returns the child's exit code."""
    with connect(autocommit=True) as conn:
        row = conn.execute(
            "SELECT agent_id, trigger_boundary, include_tail, status, kind"
            " FROM hierarchy_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        logger.error("hierarchy job {job} vanished before execution", job=job_id)
        return 1
    agent_id = int(row[0])
    trigger_boundary = str(row[1])
    include_tail = bool(row[2])
    status = str(row[3])
    kind = str(row[4])
    if kind not in (KIND_COMPACT, KIND_TAIL):
        # The DB CHECK admits exactly the known kinds; anything else is code
        # running ahead of its schema — explode rather than guess bookkeeping.
        raise ValueError(f"unknown hierarchy job kind: {kind!r}")
    if status != "running":
        # The parent owns claiming; a non-running row means a recovery raced
        # this child — nothing to do, and not an error.
        logger.warning(
            "hierarchy job {job} is {status}, not running — skipping", job=job_id, status=status
        )
        return 0

    model = agent_effective_model(agent_id, fallback=settings.lm.hierarchy_model)
    started = time.monotonic()
    try:
        # Each channel reads only its own bookkeeping target: a compact run
        # computes the cursor it may advance to; a tail run computes its seal
        # target below instead.
        advance_target = (
            _advance_target(agent_id, trigger_boundary) if kind == KIND_COMPACT else None
        )
        # The tail delta gate's value: the newest checkpoint read *before* the
        # load — a conservative lower bound of what this run seals (a write
        # after the read triggers the next job instead of being skipped, the
        # same rule as the cursor's advance target).
        tail_seal_target = latest_checkpoint_id(agent_id) if kind == KIND_TAIL else None
        known = load_known_texts(agent_id)
        deadline = started + settings.daemon.hierarchy_job_budget_seconds
        # One model for the whole job (not one per chunk): its client pool is
        # reused across the run and closed as soon as generation ends, so
        # provider sockets do not linger (task #3915). Built inside the try so
        # a construction failure still records on the job row.
        llm = build_generation_llm(model)
        try:
            tree = build_agent_tree(
                agent_id,
                llm=llm,
                model=model,
                include_tail=include_tail,
                known_texts=known,
                max_concurrent=settings.daemon.hierarchy_generation_concurrency,
                deadline=deadline,
                tools=[execute_code],
            )
        finally:
            close_chat_model(llm)
        written = write_tree(agent_id, tree.nodes, model=model)
        _record_done(job_id, agent_id, tree, written, model, advance_target, kind, tail_seal_target)
        logger.info(
            "hierarchy job {job} done: agent {agent} batches={batches} nodes={nodes}"
            " generated={generated} reused={reused} failed={failed} skipped={skipped}"
            " tokens={src}->{out} in {elapsed:.0f}s",
            job=job_id,
            agent=agent_id,
            batches=tree.batches,
            nodes=written,
            generated=tree.generated,
            reused=tree.reused,
            failed=len(tree.errors),
            skipped=tree.skipped,
            src=tree.src_tokens,
            out=tree.out_tokens,
            elapsed=time.monotonic() - started,
        )
        return 0
    except Exception:
        tail = traceback.format_exc()[-_ERROR_TAIL_CHARS:]
        _record_failed(job_id, tail)
        logger.error("hierarchy job {} failed:\n{}", job_id, tail)
        return 1


def _advance_target(agent_id: int, trigger_boundary: str) -> str:
    """The cursor value this run may advance to (see the module docstring)."""
    newest = list_compact_boundary_checkpoint_ids(agent_id, limit=1)
    if newest and newest[0] > trigger_boundary:
        return newest[0]
    return trigger_boundary


def _record_done(
    job_id: int,
    agent_id: int,
    tree: MaterializedTree,
    written: int,
    model: str,
    advance_target: str | None,
    kind: str,
    tail_seal_target: str | None,
) -> None:
    """Write the attempt's outcome; bookkeeping advances only when clean.

    A `compact` run moves the scan cursor; a `tail` run records its sealed
    stretch on `last_tail_seal_cp_id` instead (both guarded monotone, and
    both only when the run skipped nothing).
    """
    with write_transaction() as conn:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'done', finished_at = now(),"
            " model = %s, engine_version = %s, prompt_version = %s,"
            " stretches = %s, nodes = %s, generated = %s, reused = %s, failed = %s,"
            " skipped = %s, src_tokens = %s, out_tokens = %s"
            " WHERE id = %s AND status = 'running'",
            (
                model,
                ENGINE_VERSION,
                PROMPT_VERSION,
                tree.batches,
                written,
                tree.generated,
                tree.reused,
                len(tree.errors),
                tree.skipped,
                tree.src_tokens,
                tree.out_tokens,
                job_id,
            ),
        )
        if tree.skipped == 0:
            if kind == KIND_TAIL:
                if tail_seal_target is not None:
                    conn.execute(
                        "UPDATE hierarchy_worker_state"
                        " SET last_tail_seal_cp_id = %s, updated_at = now()"
                        " WHERE agent_id = %s"
                        "   AND (last_tail_seal_cp_id IS NULL OR last_tail_seal_cp_id < %s)",
                        (tail_seal_target, agent_id, tail_seal_target),
                    )
            else:  # KIND_COMPACT — execute_job rejects unknown kinds up front.
                # Fully covered: the cursor may move. The `<` guard keeps it
                # monotone even if anything ever advanced it further already.
                assert advance_target is not None  # noqa: S101 — computed for compact runs
                conn.execute(
                    "UPDATE hierarchy_worker_state"
                    " SET last_processed_boundary = %s, updated_at = now()"
                    " WHERE agent_id = %s AND last_processed_boundary < %s",
                    (advance_target, agent_id, advance_target),
                )


def _record_failed(job_id: int, error: str) -> None:
    """Park the row; no-op once it is already done/failed (recovery races)."""
    with write_transaction() as conn:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed', finished_at = now(), error = %s"
            " WHERE id = %s AND status = 'running'",
            (error, job_id),
        )
