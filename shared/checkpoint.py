"""Read an agent's stored message history from its checkpoint.

The agent's full message history (the LLM conversation: inbound envelopes,
AIMessages, tool outputs) lives in the stored checkpoint keyed by the agent
id. Gateway cold-load read paths share this single deserialized view of
`list[BaseMessage]`; this is the one place that opens the store and pulls
them out.

Compaction replaces the live messages channel, but `checkpoint_cleanup`
retains every pre-compaction checkpoint marked `compact_boundary`. Because
each retained checkpoint stores a full messages snapshot,
`load_checkpoint_messages_full` losslessly stitches the segments: it removes
only the repeated SystemMessage at a join, retaining compaction summaries and
framework session notes as part of the conversation.

Cold-load tolerance: these reads run on page-mount / ops-query paths, not on
the live-streaming path. A read can coincide with an in-flight commit; the
store returns the last committed snapshot, so a slightly stale view is
acceptable on cold-load paths.

Schema is a precondition, not a read-side effect. Fresh install creates it;
later upstream changes travel through paired Ava migrations, and ``ava start``
verifies the complete applied set. These helpers only perform SELECTs so they
also work under the least-privilege ``ava_runner`` role; a missing/outdated
schema is a store failure, never an invitation for a request path to attempt
DDL.

Read contract:
  - no checkpoint (agent never ran a turn) -> empty list.
  - store readable, latest checkpoint committed but the messages channel not
    yet written (just-spawned agent, first super-step uncommitted) -> empty
    list (legitimate state, see `load_checkpoint_messages`).
  - store unreadable (DB disconnect, deserialize error) -> raises
    CheckpointReadError. Each caller decides its own tolerance: a UI cold-load
    can swallow it to an empty view, a programmatic data endpoint surfaces it.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection, connect
from psycopg.rows import DictRow, dict_row

from shared.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from shared.config import settings

_log = logging.getLogger(__name__)


class CheckpointReadError(RuntimeError):
    """The checkpoint store could not be read or its blob could not be
    deserialized. Distinct from "no checkpoint yet" (which returns an empty
    list) and from "checkpoint present but messages channel empty" (also an
    empty list) — this is an IO / deserialize failure the caller must decide
    how to tolerate."""


def load_checkpoint_messages(agent_id: int) -> list[BaseMessage]:
    """Pull the stored message list for one agent.

    Returns the deserialized message list in conversation order, or an empty
    list when the agent has no checkpoint yet (never ran a turn) or has a
    checkpoint whose messages channel is not yet written.

    Raises:
        CheckpointReadError: the store read or blob deserialize failed
            (DB disconnect, msgpack error). The caller decides tolerance
            (see module docstring).
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    # Explicit serde allowlist — same types as the agent runner registers
    # (agent/state.py extends the static set with plugin classes). Without it
    # the permissive default warns on every read: this path deserializes the
    # whole checkpoint (all channels, incl. the nested agent.state sub-states),
    # so every timeline/messages/token-usage cold load would warn once per
    # type per gateway process start.
    serde = JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    try:
        # Direct construction rather than `from_conn_string` — the classmethod
        # does not forward a `serde` argument, and the default permissive serde
        # is exactly what this module is avoiding.
        with connect(
            settings.data_plane.db_url,
            autocommit=True,
            prepare_threshold=None,
            # psycopg's typing doesn't track row_factory — cast like the rest
            # of the codebase's pool wiring (agent/db.py).
            row_factory=cast(Any, dict_row),
        ) as conn:
            saver = PostgresSaver(conn=cast(Connection[DictRow], conn), serde=serde)
            ckpt = saver.get(config)
    except Exception as exc:
        raise CheckpointReadError(f"checkpoint read failed for agent {agent_id}") from exc
    if not ckpt:
        return []
    # `channel_values` is a guaranteed Checkpoint key (always reconstructed by
    # the store), so index it. The `messages` channel, however, can legitimately
    # be absent: a just-spawned agent's latest committed checkpoint is the input
    # snapshot, whose channel_values has not yet had the messages channel written
    # (that happens when the first super-step commits). Absence there is a valid
    # "no messages yet" state, not schema drift, so fall back to [].
    return ckpt["channel_values"].get("messages", [])


def load_checkpoint_messages_full(agent_id: int) -> list[BaseMessage]:
    """Reconstruct one agent's full history across compaction segments.

    With no retained compaction boundary, returns the latest messages snapshot
    unchanged. Otherwise, the oldest boundary is the initial segment; each
    following boundary and the latest snapshot can repeat the system prompt,
    which is dropped at the join. Every other message, including compaction
    summaries and framework session notes, remains in the conversation.

    Raises:
        CheckpointReadError: the store read or blob deserialize failed.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    serde = JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    try:
        with connect(
            settings.data_plane.db_url,
            autocommit=True,
            prepare_threshold=None,
            row_factory=cast(Any, dict_row),
        ) as conn:
            saver = PostgresSaver(conn=cast(Connection[DictRow], conn), serde=serde)
            latest = saver.get(config)
            if not latest:
                return []
            boundaries = cast(
                list[dict[str, str]],
                conn.execute(
                    "SELECT checkpoint_id FROM checkpoints"
                    " WHERE thread_id = %s AND metadata->>'compact_boundary' = 'true'"
                    " ORDER BY checkpoint_id ASC",
                    (str(agent_id),),
                ).fetchall(),
            )
            if not boundaries:
                return latest["channel_values"].get("messages", [])

            segments: list[list[BaseMessage]] = []
            for boundary in boundaries:
                checkpoint_id = boundary["checkpoint_id"]
                checkpoint_tuple = saver.get_tuple(
                    {
                        **config,
                        "configurable": {
                            **config["configurable"],
                            "checkpoint_id": checkpoint_id,
                        },
                    }
                )
                if checkpoint_tuple is not None:
                    segments.append(
                        checkpoint_tuple.checkpoint["channel_values"].get("messages", [])
                    )
    except Exception as exc:
        raise CheckpointReadError(f"full checkpoint read failed for agent {agent_id}") from exc

    if len(segments) != len(boundaries):
        raise CheckpointReadError(f"compaction boundary disappeared for agent {agent_id}")
    full_history = list(segments[0])
    latest_is_boundary = any(str(row["checkpoint_id"]) == str(latest["id"]) for row in boundaries)
    following_segments = segments[1:]
    if not latest_is_boundary:
        following_segments = [*following_segments, latest["channel_values"].get("messages", [])]
    for segment in following_segments:
        remainder = segment
        if remainder and isinstance(remainder[0], SystemMessage):
            remainder = remainder[1:]
        full_history.extend(remainder)
    return full_history


def load_checkpoint_messages_by_trace(
    agent_id: int, trace_id: str
) -> tuple[str | None, list[BaseMessage]]:
    """(checkpoint_id, messages) for the newest checkpoint of this agent's
    thread whose metadata carries `trace_id`.

    The trace↔checkpoint link is written by
    `attach_trace_to_checkpoint` after every turn (the turn's OTel trace_id
    lands in the committed checkpoint's metadata), so a trace id resolves to
    the exact checkpoint whose super-steps the trace spans. Messages include
    the system prompt (the checkpoint's `messages` channel is the full
    conversation, not a window).

    Returns (None, []) when no checkpoint carries the trace id — the trace's
    checkpoints were compact/trim pruned (checkpoint_cleanup keeps only the
    latest K), or the trace predates the link. The caller renders this as
    "pruned" rather than an error: the span metadata in the mirror/events
    still exists, only the content is gone.

    Raises:
        CheckpointReadError: store read or blob deserialize failed — same
            contract as load_checkpoint_messages.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    # Explicit serde allowlist, same as load_checkpoint_messages — without it
    # the permissive default warns on every read.
    serde = JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    try:
        # The id lookup rides a plain connection (tuple rows); the blob
        # deserialize goes through PostgresSaver.get_tuple with the resolved
        # checkpoint_id (dict rows + msgpack decode). Same db_url the
        # cold-load read path uses (AVA_DB_URL already carries the pooler port
        # when pooling is on).
        with connect(
            settings.data_plane.db_url,
            autocommit=True,
            prepare_threshold=None,
        ) as conn:
            row = conn.execute(
                "SELECT checkpoint_id FROM checkpoints"
                " WHERE thread_id = %s AND metadata->>'trace_id' = %s"
                " ORDER BY checkpoint_id DESC LIMIT 1",
                (str(agent_id), trace_id),
            ).fetchone()
        if row is None:
            return None, []
        checkpoint_id: str = row[0]
        with connect(
            settings.data_plane.db_url,
            autocommit=True,
            prepare_threshold=None,
            row_factory=cast(Any, dict_row),
        ) as conn:
            saver = PostgresSaver(conn=cast(Connection[DictRow], conn), serde=serde)
            ckpt = saver.get_tuple(
                {
                    **config,
                    "configurable": {**config["configurable"], "checkpoint_id": checkpoint_id},
                }
            )
    except Exception as exc:
        raise CheckpointReadError(
            f"checkpoint read by trace failed for agent {agent_id} trace {trace_id}"
        ) from exc
    if not ckpt:
        return checkpoint_id, []
    return checkpoint_id, ckpt.checkpoint["channel_values"].get("messages", [])


async def attach_trace_to_checkpoint(
    pool: Any,
    thread_id: str,
    checkpoint_id: str,
    trace_id: str,
) -> None:
    """Stamp `trace_id` into one checkpoint row's metadata (idempotent).

    The gateway trace endpoint resolves content on demand via
    `load_checkpoint_messages_by_trace`; the row is the checkpoint committed
    at the end of the turn whose OTel trace this trace_id belongs to.
    Failure-tolerant: a missed stamp only loses one turn's content link.
    """
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('trace_id', %s::text)"
                " WHERE thread_id = %s AND checkpoint_id = %s",
                (trace_id, thread_id, checkpoint_id),
            )
    except Exception:
        from shared.log import logger

        logger.warning(
            "failed to stamp trace_id on checkpoint",
            event="trace",
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )
