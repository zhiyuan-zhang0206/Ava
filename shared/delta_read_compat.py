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

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

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

from shared.log import logger

DELTA_COUNTERS_KEY = "counters_since_delta_snapshot"
"""Checkpoint-metadata key carrying per-channel delta replay counters.

Written by delta-capable runtimes for every checkpoint they create, except at
a snapshot step (where the counters reset to zero and the materialized
`_DeltaSnapshot` value speaks for itself). Its presence is the primary
delta-written marker.
"""


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
        return fast
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
    checkpoint["channel_values"]["messages"] = _fold_history(entry)
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
        history = checkpointer.get_delta_channel_history(
            config=_exact_config(tuple_), channels=["messages"]
        )
        entry = history.get("messages")
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
        history = await checkpointer.aget_delta_channel_history(
            config=_exact_config(tuple_), channels=["messages"]
        )
        entry = history.get("messages")
    repaired = _apply_reconstruction(checkpoint, kind, entry)
    if repaired:
        _log_reconstruction(tuple_, len(checkpoint["channel_values"]["messages"]))
    return repaired


def _log_reconstruction(tuple_: CheckpointTuple, count: int) -> None:
    configurable: dict[str, Any] = tuple_.config.get("configurable") or {}
    logger.info(
        "[{label}] {body}",
        label="delta-read-compat",
        body=(
            f"reconstructed messages for thread={configurable.get('thread_id')}"
            f" checkpoint={tuple_.checkpoint['id']}: {count} messages"
        ),
    )


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
        tuple_ = orig_get_tuple(config)
        if tuple_ is not None:
            reconstruct_delta_messages(cast("PostgresSaver", saver), tuple_)
        return tuple_

    async def aget_tuple(config: Any) -> Any:
        tuple_ = await orig_aget_tuple(config)
        if tuple_ is not None:
            await areconstruct_delta_messages(saver, tuple_)
        return tuple_

    saver.get_tuple = get_tuple  # type: ignore[method-assign]
    saver.aget_tuple = aget_tuple  # type: ignore[method-assign]
