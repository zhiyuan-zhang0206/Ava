"""Replay chunk-triggered understanding over an agent's EXISTING history (preview dev tool).

Run inside the preview container (it refuses anywhere else, so a host checkout can
never enqueue into the host's own cluster):

    docker exec -w /home/verify/.ava/source ava-preview \\
        .venv/bin/python scripts/verify/understanding_replay.py plan   AGENT_ID --ratio 0.25
    docker exec -w /home/verify/.ava/source ava-preview \\
        .venv/bin/python scripts/verify/understanding_replay.py enqueue AGENT_ID --ratio 0.25
    docker exec -w /home/verify/.ava/source ava-preview \\
        .venv/bin/python scripts/verify/understanding_replay.py report AGENT_ID

## What it does

`plan` / `enqueue` run the llm node's trigger rule (`agent/hooks/understanding_chunks.py`)
over the stored history, with the threshold `ratio x` the agent model's compact soft
threshold (`resolve_context_budget(...).soft_compact_tokens`) in place of
`AVA_UNDERSTANDING_CHUNK_TOKENS`. Every AI message's `usage_metadata.input_tokens` is the
provider-reported figure the live hook reads. A first AI turn records the baseline, a
chunk fires when the figure has grown by the threshold, and the stretch left after the
last cut closes the segment (as compaction does). `enqueue` then inserts those chunks into
`understanding_chunk_jobs`; the agent host's consumer loop processes them as for a live
agent, so the nodes and the `understanding_chunk_calls` rows are the product's own.

`consume` describes the agent's enqueued jobs itself, the compaction segments in parallel
(all at once; one segment's jobs stay in order). It runs
L1 only: the upper-level checks are skipped, because leaves of later segments land before
earlier ones finish; `regroup` afterwards builds the upper levels in message order. The
container's own host loop may claim jobs of the same agent meanwhile (it keeps one agent's
jobs in order, so the result is the same, but the jobs it finishes are followed by its own
upper-level checks; `regroup` rebuilds those too).

`report` prints, for one agent, each node (span, times, text length) and each provider call
(input, cache read, output, reasoning tokens, duration, cost-relevant totals).

## One agent per ratio

Chunk nodes are keyed `(agent_id, depth, span_start, span_end)`, so two ratios on one agent
overwrite each other wherever spans coincide. Replay each ratio on its own copy of the
agent; `enqueue` refuses an agent that already has chunk jobs.

`regroup` rebuilds the upper levels of an agent whose leaves are already described
(`hierarchy/rebuild.py`, the same rebuild a manual build queues): it drops every node above
level 1 and the grouping cursor, then replays the leaves in message order, running the
consumer's grouping checks after each — the tree grows exactly as it does when leaves land live. The raw
`understanding_group_calls` rows of earlier runs are kept (they are the comparison).

## Multi-segment agents

A history with compaction segments is cut segment by segment, each exactly as the live
trigger would: the leaves of segment k carry `compact_version` k and the indices of that
segment's request list (SystemMessage head at 0); every segment closed by a compaction
boundary ends in a closing chunk naming its boundary checkpoint; the newest segment (no
boundary yet) gets only its live chunks, its tail stays undescribed as it would live. With
the consumer running, every leaf written can trigger the upper-level grouping, so the tree
grows level by level as it would for a live agent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from langchain_core.messages import BaseMessage  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from agent.llm import execute_code  # noqa: E402
from base.agents.history.checkpoint import (  # noqa: E402
    FullHistory,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_history_full,
)
from base.agents.history.hierarchy.chunk_consumer import ModelCache, replay_jobs  # noqa: E402
from base.agents.history.hierarchy.chunk_plan import (  # noqa: E402
    PlannedChunk,
    plan_history,
    segment_requests,
)
from base.agents.history.hierarchy.chunks import enqueue_chunk, message_time  # noqa: E402
from base.agents.history.hierarchy.rebuild import run_rebuild  # noqa: E402
from base.agents.observation.snapshot import agent_model_target  # noqa: E402
from base.db import Database  # noqa: E402
from base.lm.context_budget import resolve_context_budget  # noqa: E402

_DOCKERENV = Path("/.dockerenv")


def _require_preview() -> None:
    if not _DOCKERENV.exists():
        sys.exit(
            "refusing: this tool runs inside the preview container only (docker exec ava-preview ...)"
        )


def _load(db: Database, agent_id: int) -> tuple[FullHistory, list[str]]:
    """The stitched history and the compaction boundary checkpoint ids, oldest first."""
    history = load_checkpoint_history_full(db, agent_id)
    boundaries = list(reversed(list_compact_boundary_checkpoint_ids(db, agent_id)))
    return history, boundaries


def _threshold(
    db: Database, agent_id: int, ratio: float, explicit: int | None
) -> tuple[str, int, int]:
    """(model, soft threshold, chunk threshold) for the agent."""
    model, overrides = agent_model_target(db, agent_id, fallback="")
    soft = resolve_context_budget(model, overrides).soft_compact_tokens
    return model, soft, explicit if explicit is not None else round(soft * ratio)


def _describe(
    segments: Sequence[Sequence[BaseMessage]], planned: Sequence[PlannedChunk]
) -> list[str]:
    lines: list[str] = []
    for n, p in enumerate(planned, 1):
        body = segments[p.segment][p.chunk.start_index : p.chunk.end_index]
        first, last = message_time(body, last=False), message_time(body, last=True)
        lines.append(
            f"{n:>3} seg {p.segment} [{p.chunk.start_index},{p.chunk.end_index}) {len(body):>4} msgs"
            f" tokens@end={p.input_tokens:>7} {'closing' if p.closing else 'live   '}"
            f" {first.isoformat() if first else '-'} .. {last.isoformat() if last else '-'}"
        )
    return lines


async def _enqueue(db: Database, agent_id: int, planned: Sequence[PlannedChunk]) -> None:
    pool = db.async_pool(AsyncConnectionPool, min_size=1, max_size=2, timeout=30.0)
    await pool.open()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM understanding_chunk_jobs WHERE agent_id = %s", (agent_id,)
            )
            row = await cur.fetchone()
            if row is not None and row[0]:
                sys.exit(
                    f"agent {agent_id} already has {row[0]} chunk jobs; replay each ratio on its own agent copy"
                )
        for p in planned:
            ok = await enqueue_chunk(
                pool,
                agent_id,
                compact_version=p.segment,
                chunk=p.chunk,
                end_msg_id=p.end_msg_id,
                boundary_checkpoint_id=p.boundary_checkpoint_id,
            )
            if not ok:
                sys.exit(f"enqueue failed for {p.chunk}")
    finally:
        await pool.close()


async def _consume(db: Database, agent_id: int) -> None:
    pool = db.async_pool(AsyncConnectionPool, min_size=1, max_size=32, timeout=30.0)
    await pool.open()
    try:
        print(f"agent {agent_id}: describing its jobs, every segment at once")
        await replay_jobs(pool, db, [execute_code], agent_id)
    finally:
        await pool.close()
    _report_jobs(db, agent_id)


def _report_jobs(db: Database, agent_id: int) -> None:
    with db.connect(autocommit=True) as conn:
        rows = conn.execute(
            "SELECT compact_version, status, count(*) FROM understanding_chunk_jobs"
            " WHERE agent_id = %s GROUP BY 1, 2 ORDER BY 1, 2",
            (agent_id,),
        ).fetchall()
    for segment, status, count in rows:
        print(f"  segment {segment}: {count} {status}")


async def _regroup(db: Database, agent_id: int) -> None:
    pool = db.async_pool(AsyncConnectionPool, min_size=1, max_size=3, timeout=30.0)
    await pool.open()
    try:
        leaves = await run_rebuild(pool, db, ModelCache(), agent_id)
        print(f"agent {agent_id}: upper levels rebuilt over {leaves} leaves")
    finally:
        await pool.close()


def _report(db: Database, agent_id: int) -> None:
    with db.connect(autocommit=True) as conn:
        print(
            "nodes by depth:",
            dict(
                conn.execute(
                    "SELECT depth, count(*) FROM understanding_nodes WHERE agent_id = %s GROUP BY 1 ORDER BY 1",
                    (agent_id,),
                ).fetchall()
            ),
        )
        print("nodes (span, times, chars, children, model):")
        for r in conn.execute(
            "SELECT depth, span_start, span_end, start_ts, end_ts, length(text), children_count, model"
            " FROM understanding_nodes WHERE agent_id = %s ORDER BY depth, span_start",
            (agent_id,),
        ).fetchall():
            print(
                f"  L{r[0]} [{r[1]},{r[2]}] {r[3]} .. {r[4]} chars={r[5]} children={r[6]} model={r[7]}"
            )
        g = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE problem IS NOT NULL),"
            " count(*) FILTER (WHERE round > 0), count(*) FILTER (WHERE error IS NOT NULL),"
            " coalesce(sum((usage_metadata->>'input_tokens')::int), 0),"
            " coalesce(sum(coalesce((usage_metadata->'input_token_details'->>'cache_read')::int, 0)), 0),"
            " coalesce(sum((usage_metadata->>'output_tokens')::int), 0),"
            " coalesce(sum(duration_ms), 0) / 1000.0, count(DISTINCT check_key)"
            " FROM understanding_group_calls WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        assert g is not None  # noqa: S101 - an aggregate query always returns one row
        print(
            f"group calls: {g[0]} over {g[8]} checks; refused replies {g[1]}, correction rounds {g[2]},"
            f" failed calls {g[3]}; input={g[4]} cache_read={g[5]} output={g[6]} seconds={g[7]:.1f}"
        )
        print(
            "jobs by status:",
            dict(
                conn.execute(
                    "SELECT status, count(*) FROM understanding_chunk_jobs WHERE agent_id = %s GROUP BY 1",
                    (agent_id,),
                ).fetchall()
            ),
        )
        print(
            "calls:  job round attempt  input  cache_read  output  think_chars  text_chars  seconds  error"
        )
        print(
            "(the provider does not split reasoning out of output_tokens; think_chars is the thinking block's length)"
        )
        tot = [0, 0, 0, 0, 0, 0.0]
        for r in conn.execute(
            "SELECT job_id, round, attempt,"
            " (usage_metadata->>'input_tokens')::int,"
            " coalesce((usage_metadata->'input_token_details'->>'cache_read')::int, 0),"
            " (usage_metadata->>'output_tokens')::int,"
            " coalesce((SELECT sum(length(b->>'thinking')) FROM jsonb_array_elements(content) b), 0),"
            " coalesce((SELECT sum(length(b->>'text')) FROM jsonb_array_elements(content) b), 0),"
            " duration_ms / 1000.0, error"
            " FROM understanding_chunk_calls WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        ).fetchall():
            vals = [x or 0 for x in r[3:9]]
            for k in range(6):
                tot[k] += vals[k]
            print(
                f"  {r[0]:>5} {r[1]:>3} {r[2]:>3} {vals[0]:>8} {vals[1]:>8} {vals[2]:>7}"
                f" {vals[3]:>7} {vals[4]:>7} {vals[5]:>7.1f}  {r[9] or ''}"
            )
        print(
            f"total: input={tot[0]} cache_read={tot[1]} output={tot[2]} think_chars={tot[3]}"
            f" text_chars={tot[4]} seconds={tot[5]:.1f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("plan", "enqueue", "consume", "report", "regroup"))
    parser.add_argument("agent_id", type=int)
    parser.add_argument(
        "--ratio", type=float, help="chunk threshold = ratio x the model's compact soft threshold"
    )
    parser.add_argument(
        "--threshold-tokens",
        type=int,
        help="explicit chunk threshold in tokens (overrides --ratio)",
    )
    args = parser.parse_args()
    _require_preview()
    db = Database.from_settings()
    if args.command == "report":
        _report(db, args.agent_id)
        return
    if args.command == "consume":
        asyncio.run(_consume(db, args.agent_id))
        return
    if args.command == "regroup":
        asyncio.run(_regroup(db, args.agent_id))
        return
    if args.ratio is None and args.threshold_tokens is None:
        sys.exit("--ratio or --threshold-tokens is required")
    history, boundaries = _load(db, args.agent_id)
    model, soft, threshold = _threshold(db, args.agent_id, args.ratio or 0.0, args.threshold_tokens)
    planned = plan_history(history, boundaries, threshold=threshold)
    print(
        f"agent {args.agent_id}: {len(history.messages)} messages in {len(history.segment_starts)}"
        f" segment(s), model {model}, soft {soft}, chunk threshold {threshold}"
    )
    print("\n".join(_describe(segment_requests(history), planned)))
    if args.command == "enqueue":
        asyncio.run(_enqueue(db, args.agent_id, planned))
        print(f"enqueued {len(planned)} chunk jobs")


if __name__ == "__main__":
    main()
