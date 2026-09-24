"""Delta read compatibility — reconstruct delta-written messages for plain readers.

Transition layer for the checkpoint write-model cutover (tasks #3180/#3181).
Once the `messages` channel is a LangGraph `DeltaChannel`, a stored checkpoint
usually carries no materialized `messages` value — the value lives as
per-superstep writes and is rebuilt by replaying the ancestor chain. Delta-aware
code replays on read; every other reader (vanilla-era agent code, the gateway
timeline readers, fork and impersonation copies) would instead see an empty
list, silently. This module detects a delta-written checkpoint, folds its write
chain through the message reducer, and injects the resulting plain list into
`channel_values` for those readers.

Reads only: nothing here writes to the store, and the layer is inert on
vanilla-written threads (their `messages` value is present, so nothing runs).
Like the guard shim before it, this is a deliberately removable transition
layer: delete it after the cutover has been stable for the agreed window.

Detection is narrow on purpose. A checkpoint is delta-shaped when either

* its `messages` value is absent (``MISSING``) while its metadata carries the
  delta replay counters (`counters_since_delta_snapshot`), or
* its `messages` value is a stored `_DeltaSnapshot` (a delta snapshot step).

Absence alone is NOT delta evidence — a fresh thread's first checkpoint
legitimately has no messages value — so it must never be the key.

Failure policy: reconstruction errors propagate to the caller. A reader that
kept going with an empty history could compact or rewrite over real data;
failing loudly keeps the data and makes the fault visible.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251 — recovery scope must reach LangGraph child tasks
from copy import deepcopy
from dataclasses import dataclass, field
from threading import local
from typing import Any, Literal, cast
from weakref import WeakKeyDictionary

from langchain_core.messages import BaseMessage, RemoveMessage, convert_to_messages
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import RunnableConfig
from langgraph._internal._typing import MISSING
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.errors import EmptyChannelError
from langgraph.graph.message import add_messages

from shared.checkpoint_postgres_walks import install_checkpoint_postgres_walk_patch
from shared.log import logger

install_checkpoint_postgres_walk_patch()

DELTA_COUNTERS_KEY = "counters_since_delta_snapshot"
"""Checkpoint-metadata key carrying per-channel delta replay counters.

Written by delta-capable runtimes for every checkpoint they create, except at
a snapshot step (where the counters reset to zero and the materialized
`_DeltaSnapshot` value speaks for itself). Its presence is the primary
delta-written marker.
"""


@dataclass
class _ReadSpan:
    phase: str = "tuple_read"
    tuple_read_ms: float = 0.0
    history_read_ms: float = 0.0
    stage1_pages: int = 0
    stage1_rows: int = 0
    stage2_rows: int = 0
    stage2_blob_bytes: int = 0
    decode_ms: float = 0.0
    history_build_ms: float = 0.0
    fold_ms: float = 0.0
    fold_path: str = "none"
    in_history_build: bool = False


_async_spans: WeakKeyDictionary[asyncio.Task[Any], _ReadSpan] = WeakKeyDictionary()
_sync_span = local()


@dataclass(eq=False)
class RecoveryReconstructionScope:
    """One recovery's sole message-list cache entry, never a graph state snapshot."""

    saver: AsyncPostgresSaver
    thread_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0
    key: tuple[str, str, str] | None = None
    messages: list[BaseMessage] | None = None
    active: bool = True

    def invalidate(self) -> None:
        self.generation += 1
        self.key = None
        self.messages = None


_recovery_scope: ContextVar[RecoveryReconstructionScope | None] = ContextVar(
    "delta_recovery_scope", default=None
)


def _invalidate_recovery_scopes(saver: AsyncPostgresSaver, thread_id: str) -> None:
    scopes = cast("dict[str, set[RecoveryReconstructionScope]]", saver._ava_recovery_scopes)  # type: ignore[attr-defined]
    for scope in scopes.get(thread_id, ()):
        scope.invalidate()


def _install_recovery_invalidation(saver: AsyncPostgresSaver) -> None:
    if getattr(saver, "_ava_recovery_invalidation", False):
        return
    saver._ava_recovery_invalidation = True  # type: ignore[attr-defined]
    saver._ava_recovery_scopes = {}  # type: ignore[attr-defined]
    orig_aput = saver.aput
    orig_aput_writes = saver.aput_writes
    orig_flush = getattr(saver, "_ava_nstep_flush", None)

    async def aput(config: RunnableConfig, *args: Any, **kwargs: Any) -> Any:
        thread_id = str(config["configurable"]["thread_id"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
        _invalidate_recovery_scopes(saver, thread_id)
        try:
            return await orig_aput(config, *args, **kwargs)
        finally:
            _invalidate_recovery_scopes(saver, thread_id)

    async def aput_writes(config: RunnableConfig, *args: Any, **kwargs: Any) -> Any:
        thread_id = str(config["configurable"]["thread_id"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
        _invalidate_recovery_scopes(saver, thread_id)
        try:
            return await orig_aput_writes(config, *args, **kwargs)
        finally:
            _invalidate_recovery_scopes(saver, thread_id)

    saver.aput = aput  # type: ignore[method-assign]
    saver.aput_writes = aput_writes  # type: ignore[method-assign]
    if orig_flush is not None:

        async def flush(thread_id: str) -> None:
            _invalidate_recovery_scopes(saver, thread_id)
            try:
                await orig_flush(thread_id)
            finally:
                _invalidate_recovery_scopes(saver, thread_id)

        saver._ava_nstep_flush = flush  # type: ignore[attr-defined]


@contextmanager
def recovery_reconstruction_scope(
    saver: AsyncPostgresSaver, thread_id: str
) -> Generator[RecoveryReconstructionScope | None]:
    """Bound one reconstructed checkpoint when saver reads use delta reconstruction.

    Writes invalidate every active scope for their thread, including writes from
    tasks that did not inherit this context. The one entry is cleared at exit.
    An unwrapped saver has no reconstruction to cache.
    """
    if not getattr(saver, "_ava_delta_read_compat", False):
        # A saver without delta reads has no reconstruction to cache.
        yield None
        return
    _install_recovery_invalidation(saver)
    scope = RecoveryReconstructionScope(saver=saver, thread_id=thread_id)
    scopes = cast("dict[str, set[RecoveryReconstructionScope]]", saver._ava_recovery_scopes)  # type: ignore[attr-defined]
    if scopes.get(thread_id):
        raise RuntimeError(f"concurrent checkpoint recovery for thread {thread_id}")
    scopes.setdefault(thread_id, set()).add(scope)
    token = _recovery_scope.set(scope)
    try:
        yield scope
    finally:
        scope.active = False
        scope.invalidate()
        scopes[thread_id].remove(scope)
        if not scopes[thread_id]:
            del scopes[thread_id]
        _recovery_scope.reset(token)


def _current_span() -> _ReadSpan | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return cast("_ReadSpan | None", getattr(_sync_span, "value", None))
    return _async_spans.get(task) if task is not None else None


def _set_span_phase(phase: str) -> None:
    span = _current_span()
    if span is not None:
        span.phase = phase


@contextmanager
def _bound_span(span: _ReadSpan) -> Generator[None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    previous = _async_spans.get(task) if task is not None else getattr(_sync_span, "value", None)
    if task is None:
        _sync_span.value = span
    else:
        _async_spans[task] = span
    try:
        yield
    finally:
        if task is None:
            _sync_span.value = previous
        elif previous is None:
            del _async_spans[task]
        else:
            _async_spans[task] = previous


def _coerce_group(group: Sequence[Any]) -> list[BaseMessage] | None:
    """Coerce one write group through `add_messages`' conversion pipeline.

    Returns None when the group cannot take the append fast path (a raw chunk
    or otherwise unconvertible item keeps the exact per-write fallback).
    """
    try:
        return [message_chunk_to_message(m) for m in convert_to_messages(list(group))]
    except Exception:
        return None


def _fast_append(state: Any, groups: Sequence[list[BaseMessage]]) -> list[BaseMessage] | None:
    """Append-only fast path: no removals, every id fresh and present.

    Value-identical to per-write `add_messages` application for this shape
    (each message is new, so merge order is concatenation order), at O(total
    messages) instead of O(messages x writes). Returns None on any write that
    needs the exact fallback: a `RemoveMessage`, a missing id, or an id that
    already exists (a replace).
    """
    if state is MISSING:
        base: list[BaseMessage] = []
    elif isinstance(state, list) and all(
        isinstance(m, BaseMessage) and m.id is not None for m in cast("list[Any]", state)
    ):
        base = cast("list[BaseMessage]", state)
    else:
        return None
    seen = {m.id for m in base}
    flat: list[BaseMessage] = []
    for group in groups:
        msgs = _coerce_group(group)
        if msgs is None:
            return None
        for m in msgs:
            if isinstance(m, RemoveMessage) or m.id is None or m.id in seen:
                return None
            seen.add(m.id)
            flat.append(m)
    return [*base, *flat]


def _fold_messages(state: Any, writes: Sequence[Any]) -> Any:
    """`DeltaChannel` reducer: fold stored write values into the message list.

    Each write is a list of message-likes (or one message-like). Append-only
    folds take the fast path; everything else falls back to per-write
    `add_messages`, which is the exact semantics of the guarded production
    reducer minus its validation (the writes were validated at commit time).
    """
    groups: list[list[Any]] = [w if isinstance(w, list) else [w] for w in writes]
    fast = _fast_append(state, groups)
    if fast is not None:
        span = _current_span()
        if span is not None:
            span.fold_path = "fast"
        return fast
    span = _current_span()
    if span is not None:
        span.fold_path = "fallback"
    result: Any = [] if state is MISSING else state
    for group in groups:
        result = add_messages(result, group)
    return result


def _fold_history(entry: Mapping[str, Any]) -> list[BaseMessage]:
    """Fold one `DeltaChannelHistory` entry into the channel value.

    A `DeltaChannel` gives the exact runtime semantics for free: `seed` may be
    missing, a plain list, or a `_DeltaSnapshot` (unwrapped), and an
    `Overwrite` write resets the base the same way the runtime's replay does.
    """
    channel = DeltaChannel[Any](_fold_messages).from_checkpoint(entry.get("seed", MISSING))
    channel.replay_writes(entry["writes"])
    try:
        return cast("list[BaseMessage]", channel.get())
    except EmptyChannelError:
        return []


def _repair_kind(
    checkpoint: Mapping[str, Any], metadata: Mapping[str, Any]
) -> Literal["snapshot", "walk"] | None:
    """Which reconstruction (if any) this checkpoint needs, per module docstring."""
    stored = checkpoint["channel_values"].get("messages", MISSING)
    if isinstance(stored, _DeltaSnapshot):
        return "snapshot"
    if stored is not MISSING:
        return None
    if "messages" not in checkpoint["channel_versions"]:
        return None
    if DELTA_COUNTERS_KEY not in (metadata or {}):
        return None
    return "walk"


def _exact_config(tuple_: CheckpointTuple) -> RunnableConfig:
    """The tuple's config with its resolved checkpoint id made explicit.

    The explicit id keeps the history walk on its own SQL path: without it the
    saver resolves the id through `aget_tuple`, which the read wrapper patches
    (harmless but redundant work, and one more chance to re-enter repair).
    """
    configurable: dict[str, Any] = dict(tuple_.config.get("configurable") or {})
    configurable.setdefault("checkpoint_id", tuple_.checkpoint["id"])
    configurable.setdefault("checkpoint_ns", "")
    return cast("RunnableConfig", {"configurable": configurable})


def _apply_reconstruction(
    checkpoint: dict[str, Any], kind: Literal["snapshot", "walk"], entry: Mapping[str, Any] | None
) -> bool:
    """Replace a delta checkpoint's messages value with its plain equivalent."""
    if kind == "snapshot":
        snapshot = cast("_DeltaSnapshot", checkpoint["channel_values"]["messages"])
        checkpoint["channel_values"]["messages"] = cast("list[BaseMessage]", snapshot.value)
        return True
    if entry is None or (not entry.get("writes") and "seed" not in entry):
        return False
    started = time.monotonic()
    span = _current_span()
    if span is not None:
        span.phase = "fold"
    try:
        checkpoint["channel_values"]["messages"] = _fold_history(entry)
    finally:
        if span is not None:
            span.fold_ms = (time.monotonic() - started) * 1000
    return True


def reconstruct_delta_messages(checkpointer: PostgresSaver, tuple_: CheckpointTuple) -> bool:
    """Materialize a delta-written checkpoint's `messages` value, in place.

    Returns True when a value was injected. Safe to call on every read: it
    returns immediately for vanilla-written checkpoints and needs no saver
    call for a stored `_DeltaSnapshot` (which is unwrapped, not folded).
    """
    checkpoint = cast("dict[str, Any]", tuple_.checkpoint)
    kind = _repair_kind(checkpoint, tuple_.metadata or {})
    if kind is None:
        return False
    entry: Mapping[str, Any] | None = None
    if kind == "walk":
        started = time.monotonic()
        span = _current_span()
        if span is not None:
            span.phase = "history_read"
        try:
            history = checkpointer.get_delta_channel_history(
                config=_exact_config(tuple_), channels=["messages"]
            )
        finally:
            if span is not None:
                span.history_read_ms = (time.monotonic() - started) * 1000
        entry = history.get("messages")
    elif (span := _current_span()) is not None:
        span.phase = "snapshot"
    repaired = _apply_reconstruction(checkpoint, kind, entry)
    if repaired:
        _log_reconstruction(tuple_, len(checkpoint["channel_values"]["messages"]))
    return repaired


async def areconstruct_delta_messages(
    checkpointer: AsyncPostgresSaver, tuple_: CheckpointTuple
) -> bool:
    """Async twin of `reconstruct_delta_messages`."""
    checkpoint = cast("dict[str, Any]", tuple_.checkpoint)
    kind = _repair_kind(checkpoint, tuple_.metadata or {})
    if kind is None:
        return False
    entry: Mapping[str, Any] | None = None
    if kind == "walk":
        started = time.monotonic()
        span = _current_span()
        if span is not None:
            span.phase = "history_read"
        try:
            history = await checkpointer.aget_delta_channel_history(
                config=_exact_config(tuple_), channels=["messages"]
            )
        finally:
            if span is not None:
                span.history_read_ms = (time.monotonic() - started) * 1000
        entry = history.get("messages")
    elif (span := _current_span()) is not None:
        span.phase = "snapshot"
    repaired = _apply_reconstruction(checkpoint, kind, entry)
    if repaired:
        _log_reconstruction(tuple_, len(checkpoint["channel_values"]["messages"]))
    return repaired


async def _areconstruct_in_recovery(
    saver: AsyncPostgresSaver, tuple_: CheckpointTuple, read_generation: int | None
) -> bool:
    scope = _recovery_scope.get()
    configurable = tuple_.config["configurable"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
    if (
        scope is None
        or not scope.active
        or scope.saver is not saver
        or scope.thread_id != str(configurable["thread_id"])
        or _repair_kind(tuple_.checkpoint, tuple_.metadata or {}) != "walk"
    ):
        return await areconstruct_delta_messages(saver, tuple_)

    key = (
        str(configurable["thread_id"]),
        tuple_.checkpoint["id"],
        configurable.get("checkpoint_ns", ""),
    )
    _set_span_phase("cache_lock")
    async with scope.lock:
        checkpoint = cast("dict[str, Any]", tuple_.checkpoint)
        if scope.key == key and scope.messages is not None and read_generation == scope.generation:
            _set_span_phase("cache_copy")
            checkpoint["channel_values"]["messages"] = deepcopy(scope.messages)
            _log_reconstruction(tuple_, len(scope.messages), cache_hit=True)
            return True
        generation = scope.generation
        repaired = await areconstruct_delta_messages(saver, tuple_)
        if repaired and scope.active and scope.generation == generation == read_generation:
            scope.key = key
            scope.messages = deepcopy(checkpoint["channel_values"]["messages"])
        return repaired


def _log_reconstruction(
    tuple_: CheckpointTuple | None,
    count: int | None,
    *,
    config: RunnableConfig | None = None,
    cache_hit: bool | None = False,
    outcome: Literal["success", "error", "cancelled"] = "success",
    failed_phase: str | None = None,
    elapsed_ms: float | None = None,
    error_type: str | None = None,
) -> None:
    source_config = tuple_.config if tuple_ is not None else config or {}
    configurable: dict[str, Any] = source_config.get("configurable") or {}
    checkpoint_id = (
        tuple_.checkpoint["id"] if tuple_ is not None else configurable.get("checkpoint_id")
    )
    span = _current_span() or _ReadSpan()
    logger.info(
        "[{label}] {body}",
        label="delta-read-compat",
        event="delta_read_compat",
        thread_id=configurable.get("thread_id"),
        checkpoint_id=checkpoint_id,
        checkpoint_ns=configurable.get("checkpoint_ns", ""),
        message_count=count,
        cache_hit=cache_hit,
        outcome=outcome,
        failed_phase=failed_phase,
        elapsed_ms=elapsed_ms,
        error_type=error_type,
        tuple_read_ms=span.tuple_read_ms,
        history_read_ms=span.history_read_ms,
        stage1_pages=span.stage1_pages,
        stage1_rows=span.stage1_rows,
        stage2_rows=span.stage2_rows,
        stage2_blob_bytes=span.stage2_blob_bytes,
        decode_ms=span.decode_ms,
        history_build_ms=span.history_build_ms,
        fold_ms=span.fold_ms,
        fold_path=span.fold_path,
        body=(
            f"{'reused' if cache_hit else 'reconstructed'} messages for thread={configurable.get('thread_id')}"
            f" checkpoint={checkpoint_id}: {count} messages"
            if outcome == "success"
            else f"{outcome} during {failed_phase} for thread={configurable.get('thread_id')}"
            f" checkpoint={checkpoint_id}"
        ),
    )


def _instrument_history_callbacks(saver: AsyncPostgresSaver) -> None:
    """Count the rows and bytes LangGraph has already fetched for a history walk."""
    orig_ingest = saver._ingest_stage1_page
    orig_build = saver._build_delta_channels_writes_history
    serde = saver.serde
    if not getattr(serde, "_ava_delta_decode_instrumented", False):
        orig_decode = serde.loads_typed

        def decode(value: Any) -> Any:
            span = _current_span()
            if span is None or not span.in_history_build:
                return orig_decode(value)
            started = time.monotonic()
            span.phase = "decode"
            try:
                result = orig_decode(value)
                span.phase = "history_build"
                return result
            finally:
                span.decode_ms += (time.monotonic() - started) * 1000

        serde.loads_typed = decode  # type: ignore[method-assign]
        serde._ava_delta_decode_instrumented = True  # type: ignore[attr-defined]

    def ingest_stage1_page(*args: Any, **kwargs: Any) -> Any:
        span = _current_span()
        if span is not None:
            span.stage1_pages += 1
            span.stage1_rows += len(args[0])
        return orig_ingest(*args, **kwargs)

    def build_history(*args: Any, **kwargs: Any) -> Any:
        span = _current_span()
        if span is None:
            return orig_build(*args, **kwargs)
        rows = kwargs["stage2_rows"]
        span.stage2_rows = len(rows)
        span.stage2_blob_bytes = sum(len(row["blob"]) for row in rows)
        started = time.monotonic()
        span.in_history_build = True
        span.phase = "history_build"
        try:
            result = orig_build(*args, **kwargs)
            span.phase = "history_read"
            return result
        finally:
            span.in_history_build = False
            span.history_build_ms = (time.monotonic() - started) * 1000

    saver._ingest_stage1_page = ingest_stage1_page  # type: ignore[method-assign]
    saver._build_delta_channels_writes_history = build_history  # type: ignore[method-assign]


def wrap_saver_reads_with_delta_reconstruction(saver: AsyncPostgresSaver) -> None:
    """Patch one saver instance so every tuple read returns reconstructed state.

    Both read entry points are patched (`get_tuple` / `aget_tuple`), so every
    caller — the pregel load, `aget_state`/`get_state` and their history
    walks, the startup inbound reconciler (`aget`) — is covered from one
    place. Idempotent, and the same instance-level patch pattern as the
    host's write wrappers. The history walk runs on the saver's own
    `get_delta_channel_history` path (an exact-checkpoint-id config), so it
    never re-enters the patched reads.
    """
    if getattr(saver, "_ava_delta_read_compat", False):
        return
    saver._ava_delta_read_compat = True  # type: ignore[attr-defined]
    orig_get_tuple = saver.get_tuple
    orig_aget_tuple = saver.aget_tuple

    def get_tuple(config: Any) -> Any:
        span = _ReadSpan()
        with _bound_span(span):
            started = time.monotonic()
            tuple_ = None
            try:
                try:
                    tuple_ = orig_get_tuple(config)
                finally:
                    span.tuple_read_ms = (time.monotonic() - started) * 1000
                if tuple_ is not None:
                    span.phase = "repair_detection"
                    reconstruct_delta_messages(cast("PostgresSaver", saver), tuple_)
                return tuple_
            except Exception as exc:
                _log_reconstruction(
                    tuple_,
                    None,
                    config=config,
                    cache_hit=None,
                    outcome="error",
                    failed_phase=span.phase,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    error_type=type(exc).__name__,
                )
                raise

    async def aget_tuple(config: Any) -> Any:
        span = _ReadSpan()
        with _bound_span(span):
            scope = _recovery_scope.get()
            read_generation = scope.generation if scope is not None else None
            started = time.monotonic()
            tuple_ = None
            try:
                try:
                    tuple_ = await orig_aget_tuple(config)
                finally:
                    span.tuple_read_ms = (time.monotonic() - started) * 1000
                if tuple_ is not None:
                    span.phase = "repair_detection"
                    await _areconstruct_in_recovery(saver, tuple_, read_generation)
                return tuple_
            except (Exception, asyncio.CancelledError) as exc:
                _log_reconstruction(
                    tuple_,
                    None,
                    config=config,
                    cache_hit=None,
                    outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                    failed_phase=span.phase,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    error_type=type(exc).__name__,
                )
                raise

    _instrument_history_callbacks(saver)
    saver.get_tuple = get_tuple  # type: ignore[method-assign]
    saver.aget_tuple = aget_tuple  # type: ignore[method-assign]
