"""Pinned checkpoint-postgres read adapters: pagination and message reset boundaries.

Remove the pagination override after langgraph#8448 / #8556 ships in a stable
release. The messages-only reader additionally stops body transfer at a reset
inside serialized writes; retain it until upstream supports that boundary.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from importlib.metadata import version
from inspect import Parameter, signature
from typing import Any, LiteralString, cast

from langchain_core.messages import RemoveMessage, convert_to_messages
from langchain_core.runnables import RunnableConfig
from langgraph.channels.binop import _get_overwrite
from langgraph.checkpoint.base import DeltaChannelHistory, get_checkpoint_id
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver, _DeltaStage2Row
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from base.log import logger

_EXPECTED_PARAMETERS = (
    "target_id",
    "channels",
    "parent_of",
    "ver_by_i_by_cid",
    "hb_by_i_by_cid",
    "inline_by_i_by_cid",
    "chain_by_ch",
    "seed_ver_by_ch",
    "seed_inline_by_ch",
    "walk_cursor_by_ch",
    "seeded",
)


def validate_checkpoint_postgres_api() -> None:
    """Fail at Ava saver construction if its pinned private extension seam changes."""
    dependency_version = version("langgraph-checkpoint-postgres")
    if dependency_version != "3.1.2":
        raise RuntimeError(
            "checkpoint-postgres adapter requires langgraph-checkpoint-postgres==3.1.2; "
            f"found {dependency_version}"
        )
    descriptor = vars(BasePostgresSaver)["_try_advance_walks"]
    if not isinstance(descriptor, staticmethod):
        raise TypeError("checkpoint-postgres _try_advance_walks was replaced outside Ava")
    original = descriptor.__func__
    method_signature = signature(original)
    if (
        original.__module__ != "langgraph.checkpoint.postgres.base"
        or original.__qualname__ != "BasePostgresSaver._try_advance_walks"
        or hasattr(original, "__wrapped__")
        or tuple(method_signature.parameters) != _EXPECTED_PARAMETERS
        or any(
            parameter.kind is not Parameter.POSITIONAL_OR_KEYWORD
            or parameter.default is not Parameter.empty
            for parameter in method_signature.parameters.values()
        )
        or method_signature.return_annotation not in (None, "None")
    ):
        raise RuntimeError("checkpoint-postgres _try_advance_walks signature or identity changed")


class CheckpointWalks:
    """Ava-owned pagination extension; upstream classes remain unchanged."""

    @staticmethod
    def _try_advance_walks(
        target_id: str,
        channels: Sequence[str],
        parent_of: Mapping[str, str | None],
        ver_by_i_by_cid: Sequence[Mapping[str, str | None]],
        hb_by_i_by_cid: Sequence[Mapping[str, bool]],
        inline_by_i_by_cid: Sequence[Mapping[str, Any]],
        chain_by_ch: dict[str, list[str]],
        seed_ver_by_ch: dict[str, str | None],
        seed_inline_by_ch: dict[str, Any],
        walk_cursor_by_ch: dict[str, str | None],
        seeded: set[str],
    ) -> None:
        if target_id not in parent_of:
            return
        BasePostgresSaver._try_advance_walks(
            target_id,
            channels,
            parent_of,
            ver_by_i_by_cid,
            hb_by_i_by_cid,
            inline_by_i_by_cid,
            chain_by_ch,
            seed_ver_by_ch,
            seed_inline_by_ch,
            walk_cursor_by_ch,
            seeded,
        )


# task #3696 exception inventory: transport read guards, not conversation limits.
# A quarter-MiB batch bounds reset overfetch while reading the measured ~1-MiB
# active histories in a handful of requests. At most 128 usual UUID write keys
# keep the three composite-key request arrays around 12 KiB, including tiny
# or empty writes. One indivisible oversized value is fetched intact; neither
# guard truncates state or changes replay semantics, so these are not user knobs.
_BODY_BATCH_BYTES = 256 * 1024
_BODY_BATCH_ROWS = 128

_ANCESTOR_COLUMNS = """
    c.checkpoint_id, c.parent_checkpoint_id,
    c.checkpoint -> 'channel_versions' ->> 'messages' AS ver_0,
    EXISTS (SELECT 1 FROM checkpoint_blobs b
            WHERE b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
              AND b.channel = 'messages'
              AND b.version = c.checkpoint -> 'channel_versions' ->> 'messages'
              AND b.type <> 'empty') AS hb_0,
    c.checkpoint -> 'channel_values' -> 'messages' AS inline_0
"""
_ANCESTORS_SQL = f"""
WITH RECURSIVE ancestors AS (
    SELECT {_ANCESTOR_COLUMNS}, 0 AS depth
    FROM checkpoints c
    WHERE c.thread_id = %s AND c.checkpoint_ns = %s AND c.checkpoint_id = %s
    UNION ALL
    SELECT {_ANCESTOR_COLUMNS}, a.depth + 1
    FROM ancestors a JOIN checkpoints c ON c.checkpoint_id = a.parent_checkpoint_id
    WHERE c.thread_id = %s AND c.checkpoint_ns = %s
      AND (a.depth = 0 OR NOT (a.hb_0 OR a.inline_0 IS NOT NULL))
)
SELECT * FROM ancestors ORDER BY depth
"""  # noqa: S608 — only a static column list is interpolated
_WRITE_KEYS_SQL = """
SELECT w.checkpoint_id, w.task_id, w.idx, octet_length(w.blob) AS size
FROM unnest(%s::text[]) WITH ORDINALITY AS chain(checkpoint_id, depth)
JOIN checkpoint_writes w USING (checkpoint_id)
WHERE w.thread_id = %s AND w.checkpoint_ns = %s AND w.channel = 'messages'
ORDER BY chain.depth, w.task_id DESC, w.idx DESC
"""
_WRITE_BODIES_SQL = """
SELECT 'w'::text AS _kind, w.checkpoint_id, w.channel, w.type, w.blob,
       w.task_id, w.idx, NULL::text AS version
FROM unnest(%s::text[], %s::text[], %s::int[]) WITH ORDINALITY
     AS wanted(checkpoint_id, task_id, idx, ordinal)
JOIN checkpoint_writes w USING (checkpoint_id, task_id, idx)
WHERE w.thread_id = %s AND w.checkpoint_ns = %s AND w.channel = 'messages'
ORDER BY wanted.ordinal
"""
_SEED_SQL = """
SELECT 'b'::text AS _kind, NULL::text AS checkpoint_id, channel, type, blob,
       NULL::text AS task_id, NULL::int AS idx, version
FROM checkpoint_blobs
WHERE thread_id = %s AND checkpoint_ns = %s AND channel = 'messages' AND version = %s
"""

_Query = tuple[LiteralString, Sequence[Any]]
_Rows = list[dict[str, Any]]


def _body_batches(keys: _Rows) -> Generator[_Rows]:
    batch: _Rows = []
    size = 0
    for key in keys:
        if batch and (size + key["size"] > _BODY_BATCH_BYTES or len(batch) >= _BODY_BATCH_ROWS):
            yield batch
            batch, size = [], 0
        batch.append(key)
        size += key["size"]
    if batch:
        yield batch


def _resets_messages(value: Any) -> bool:
    if _get_overwrite(value)[0]:
        return True
    group = cast(list[Any], value) if isinstance(value, list) else [value]
    messages = convert_to_messages(group)
    return any(isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES for m in messages)


def _fetch_suffix(
    saver: BasePostgresSaver, keys: _Rows, thread: str, namespace: str, target: str
) -> Generator[_Query, _Rows, tuple[_Rows, bool]]:
    rows: _Rows = []
    body_bytes = body_rows = body_batches = 0
    reset = False
    decode = saver.serde.loads_typed
    for batch in _body_batches(keys):
        fetched = yield (
            _WRITE_BODIES_SQL,
            (
                [k["checkpoint_id"] for k in batch],
                [k["task_id"] for k in batch],
                [k["idx"] for k in batch],
                thread,
                namespace,
            ),
        )
        if len(fetched) != len(batch):
            raise RuntimeError("message history changed while reading its write bodies")
        body_batches += 1
        body_rows += len(fetched)
        body_bytes += sum(len(row["blob"]) for row in fetched)
        for row in fetched:
            rows.append(row)
            if _resets_messages(decode((row["type"], row["blob"]))):
                reset = True
                break
        if reset:
            break
    logger.info(
        "read message history suffix",
        event="delta_message_suffix",
        thread_id=thread,
        checkpoint_ns=namespace,
        checkpoint_id=target,
        candidate_writes=len(keys),
        fetched_rows=body_rows,
        fetched_bytes=body_bytes,
        body_batches=body_batches,
        retained_writes=len(rows),
        reset_found=reset,
    )
    return rows, reset


def _history_queries(
    saver: BasePostgresSaver, config: Mapping[str, Any]
) -> Generator[_Query, _Rows, dict[str, DeltaChannelHistory]]:
    """Share exact query, ordering and reset semantics across both saver APIs."""
    identity = config["configurable"]
    thread, namespace = identity["thread_id"], identity.get("checkpoint_ns", "")
    target = identity["checkpoint_id"]
    ancestors = yield _ANCESTORS_SQL, (thread, namespace, target, thread, namespace)
    parents: dict[str, str | None] = {}
    versions: list[dict[str, str | None]] = [{}]
    blobs: list[dict[str, bool]] = [{}]
    inline: list[dict[str, Any]] = [{}]
    chains: dict[str, list[str]] = {"messages": []}
    seeds: dict[str, str | None] = {"messages": None}
    seed_inline: dict[str, Any] = {}
    saver._ingest_stage1_page(ancestors, ["messages"], parents, versions, blobs, inline)
    saver._try_advance_walks(
        target,
        ["messages"],
        parents,
        versions,
        blobs,
        inline,
        chains,
        seeds,
        seed_inline,
        {},
        set(),
    )
    chain = chains["messages"]
    keys = yield _WRITE_KEYS_SQL, (chain, thread, namespace)
    rows, reset = yield from _fetch_suffix(saver, keys, thread, namespace, target)
    if reset:
        seeds["messages"] = None
        seed_inline.clear()
    if seeds["messages"] is not None and "messages" not in seed_inline:
        seed_rows = yield _SEED_SQL, (thread, namespace, seeds["messages"])
        rows.extend(seed_rows)
    return saver._build_delta_channels_writes_history(
        channels=["messages"],
        chain_by_ch=chains,
        seed_ver_by_ch=seeds,
        seed_inline_by_ch=seed_inline,
        stage2_rows=cast(list[_DeltaStage2Row], rows),
    )


class HistoryPostgresSaver(CheckpointWalks, PostgresSaver):
    """Explicit sync checkpoint reader with pinned pagination and reset suffix reads."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        validate_checkpoint_postgres_api()
        super().__init__(*args, **kwargs)

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        if list(channels) != ["messages"] or get_checkpoint_id(config) is None:
            return super().get_delta_channel_history(config=config, channels=channels)
        queries = _history_queries(self, config)
        rows: _Rows = []
        started = False
        while True:
            try:
                sql, params = queries.send(rows) if started else next(queries)
                started = True
            except StopIteration as result:
                return result.value
            with self._cursor() as cursor:
                cursor.execute(sql, params, binary=True)
                rows = cursor.fetchall()


class HistoryAsyncPostgresSaver(CheckpointWalks, AsyncPostgresSaver):
    """Explicit async checkpoint reader; sync calls use upstream's loop bridge."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        validate_checkpoint_postgres_api()
        super().__init__(*args, **kwargs)

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        if list(channels) != ["messages"] or get_checkpoint_id(config) is None:
            return await super().aget_delta_channel_history(config=config, channels=channels)
        queries = _history_queries(self, config)
        rows: _Rows = []
        started = False
        while True:
            try:
                sql, params = queries.send(rows) if started else next(queries)
                started = True
            except StopIteration as result:
                return result.value
            async with self._cursor() as cursor:
                await cursor.execute(sql, params, binary=True)
                rows = await cursor.fetchall()
