"""Fork's checkpoint copy: one agent's LangGraph state, windowed at a compaction boundary, as another's first checkpoints.

Pure checkpoint SQL over the `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` tables, so it lives with the
rest of the checkpoint history code; `ops.agents.spawn` calls it inside the fork's row-creation transaction.
"""

from __future__ import annotations

import psycopg

from base.agents import ForkCheckpointNotFound


def copy_checkpoint_chain(
    cur: psycopg.Cursor,
    source_agent_id: int,
    source_checkpoint_id: str,
    new_agent_id: int,
) -> None:
    """Copy the source agent's LangGraph state at source_checkpoint_id as the
    new_agent's first checkpoints.

    The copy is one segment window: the ancestor walk stops at (and includes)
    the nearest compaction boundary strictly below the fork point, so its size
    is bounded by at most two compacted segments instead of the thread's whole
    history. The fork checkpoint itself never terminates the walk — when it is
    itself a boundary the walk continues down to the next boundary below —
    because a boundary is NOT a self-contained snapshot on a delta-written
    thread: it is an ordinary checkpoint stamped `compact_boundary` by
    `mark_compact_boundary`, and its content is rebuilt by folding the write
    chain. Cutting the window AT a boundary could contain neither a
    materialized snapshot nor the compaction reset (REMOVE_ALL) that opened
    the fork point's segment, and the replica read back empty (task #3979). A
    thread that never compacted has no boundary and is copied back to its root
    — which is that thread's whole history, still one context window's worth.
    Chains are retained in full by design (delta-written threads are exempt
    from checkpoint trimming — the never-delete ruling, tasks #3180/#3181),
    so chain length tracks a thread's whole life. Bounding the copy is what keeps repeated forks O(N) in rows
    written rather than O(N^2); an unbounded copy also eventually exceeds
    `statement_timeout`.

    Rows copied:
    - **checkpoints**: target ckpt_id and its ancestors, recursively following
      parent_checkpoint_id, stopping at (and including) the first
      `compact_boundary` checkpoint strictly below the target (the target
      itself always walks past)
    - **checkpoint_blobs**: only the (ns, channel, version) triples the copied
      checkpoints actually reference through their `channel_versions` — the
      same join PostgresSaver's SELECT_SQL uses to read them, so nothing a
      reader can reach is left behind
    - **checkpoint_writes**: every write row attached to a copied checkpoint,
      so the replica is complete for delta-written threads — their message
      content lives in the writes, not blobs (tasks #3180/#3181). For
      full-snapshot threads the copy is invisible to readers (values still
      come from blobs) and keeps the same resume semantics: writes attach
      to their producing checkpoint, and a clean tip has none attached.

    LangGraph checkpoints / blobs tables have `thread_id` as TEXT (framework
    schema unchanged) — agents_meta.id is BIGINT, this function explicitly
    str()-casts when writing into thread_id.

    Raises:
        ForkCheckpointNotFound: source_checkpoint_id does not exist on the
            source agent (recursive CTE INSERT 0 rows).
    """
    # LangGraph schema preserved: checkpoints.thread_id / checkpoint_blobs.thread_id
    # are framework column names (hardcoded in PostgresSaver); we cast Ava agent_id to str() here.
    cur.execute(
        """
        WITH RECURSIVE chain AS (
            SELECT * FROM checkpoints
             WHERE thread_id = %(src)s AND checkpoint_id = %(ckpt)s
            UNION ALL
            SELECT c.*
              FROM checkpoints c
              JOIN chain ON c.thread_id = %(src)s
                       AND c.checkpoint_id = chain.parent_checkpoint_id
             -- Walk past a row only when it is not a boundary — except the
             -- fork row itself, which never terminates its own walk: when the
             -- fork checkpoint IS a boundary, the walk continues down to the
             -- next boundary below it, so the copied window contains the reset
             -- that opened the fork point's segment (a boundary is metadata on
             -- an ordinary checkpoint, not a self-contained snapshot).
             WHERE chain.checkpoint_id = %(ckpt)s
                OR NOT COALESCE(
                 (chain.metadata ->> 'compact_boundary')::boolean, false
             )
        )
        INSERT INTO checkpoints (
            thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
            type, checkpoint, metadata
        )
        SELECT %(new)s, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
               type, checkpoint, metadata
          FROM chain
        """,
        {"src": str(source_agent_id), "ckpt": source_checkpoint_id, "new": str(new_agent_id)},
    )
    if cur.rowcount == 0:
        raise ForkCheckpointNotFound(
            f"checkpoint {source_checkpoint_id!r} does not exist in thread {source_agent_id}"
        )
    cur.execute(
        """
        WITH RECURSIVE chain AS (
            SELECT checkpoint_id, parent_checkpoint_id, metadata FROM checkpoints
             WHERE thread_id = %(src)s AND checkpoint_id = %(ckpt)s
            UNION ALL
            SELECT c.checkpoint_id, c.parent_checkpoint_id, c.metadata
              FROM checkpoints c
              JOIN chain ON c.thread_id = %(src)s
                       AND c.checkpoint_id = chain.parent_checkpoint_id
             WHERE chain.checkpoint_id = %(ckpt)s
                OR NOT COALESCE(
                 (chain.metadata ->> 'compact_boundary')::boolean, false
             )
        )
        INSERT INTO checkpoint_writes (
            thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob
        )
        SELECT %(new)s, w.checkpoint_ns, w.checkpoint_id, w.task_id, w.idx,
               w.channel, w.type, w.blob
          FROM checkpoint_writes w
          JOIN chain ON chain.checkpoint_id = w.checkpoint_id
         WHERE w.thread_id = %(src)s
        """,
        {"src": str(source_agent_id), "ckpt": source_checkpoint_id, "new": str(new_agent_id)},
    )
    cur.execute(
        """
        WITH RECURSIVE chain AS (
            SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                   checkpoint, metadata
              FROM checkpoints
             WHERE thread_id = %(src)s AND checkpoint_id = %(ckpt)s
            UNION ALL
            SELECT c.checkpoint_ns, c.checkpoint_id, c.parent_checkpoint_id,
                   c.checkpoint, c.metadata
              FROM checkpoints c
              JOIN chain ON c.thread_id = %(src)s
                       AND c.checkpoint_id = chain.parent_checkpoint_id
             WHERE chain.checkpoint_id = %(ckpt)s
                OR NOT COALESCE(
                 (chain.metadata ->> 'compact_boundary')::boolean, false
             )
        ),
        referenced AS (
            -- The triples a reader can reach: PostgresSaver's SELECT_SQL joins
            -- blobs through exactly this `channel_versions` mapping.
            SELECT DISTINCT chain.checkpoint_ns AS checkpoint_ns,
                   cv.key AS channel, cv.value AS version
              FROM chain
              CROSS JOIN LATERAL jsonb_each_text(
                  chain.checkpoint -> 'channel_versions'
              ) AS cv
        )
        INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
        SELECT %(new)s, b.checkpoint_ns, b.channel, b.version, b.type, b.blob
          FROM checkpoint_blobs b
          JOIN referenced r ON r.checkpoint_ns = b.checkpoint_ns
                           AND r.channel = b.channel
                           AND r.version = b.version
         WHERE b.thread_id = %(src)s
        """,
        {"src": str(source_agent_id), "ckpt": source_checkpoint_id, "new": str(new_agent_id)},
    )
