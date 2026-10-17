"""One-shot startup helpers run before the graph loop begins.

- `wrap_saver_writes_with_loud_failure` — monkey-patch checkpointer aput
  / aput_writes to log every real failure as `checkpoint_write_failed` before
  re-raising (LangGraph internally swallows aput failures otherwise); a
  cooperative cancellation passes through unlogged
- `wrap_saver_writes_with_nstep_interval` — throttle checkpoint writes to
  every Nth super-step while keeping aput_writes in lockstep and exposing a
  final-state flush
- `consume_claimed_inbounds` — consume any 'claimed' chat inbound rows the
  agent's last runtime step left behind (cold admission, database recovery,
  or a hosted turn's end or abort); never redelivered
- `notify_desktop_permissions_at_startup` — surface broken Screen Recording
  or Accessibility permission (detected at converge) to the user, exactly once
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.constants import PUSH
from psycopg_pool import AsyncConnectionPool

from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation


def wrap_saver_writes_with_loud_failure(
    checkpointer: AsyncPostgresSaver, agent_id: int | None = None
) -> None:
    """Wrap aput / aput_writes to log every real failure as a
    checkpoint_write_failed event before re-raising (a cooperative
    CancelledError is control flow and propagates unlogged). See call site for
    the langgraph silent-swallow background-task path this defends against.

    The two methods are monkey-patched on the instance — langgraph never
    re-binds them after `__init__`, so this is safe across the saver
    lifetime. Wrapper re-raises to keep langgraph's own retry / propagation
    semantics intact; the only added side effect is a log line per failure.
    Identity is resolved from each write's thread_id, including deferred N-step
    flushes. An explicit single-agent id is only a consistency constraint;
    neither an ambient turn nor a daemon-global identity attributes shared writes.
    """
    orig_aput = checkpointer.aput
    orig_aput_writes = checkpointer.aput_writes

    def write_agent_id(config: RunnableConfig) -> int:
        resolved = int(_checkpoint_thread_id(config))
        if agent_id is not None and resolved != agent_id:
            raise ValueError("checkpoint thread_id differs from the saver agent identity")
        return resolved

    async def _logged_aput(config: RunnableConfig, *args: Any, **kwargs: Any) -> Any:
        owner = write_agent_id(config)
        try:
            return await orig_aput(config, *args, **kwargs)
        except asyncio.CancelledError:
            raise  # cancellation is control flow, not a failed write (task #4964)
        except BaseException:
            logger.opt(exception=True).error(
                "checkpoint aput failed",
                event="checkpoint_write_failed",
                agent_id=owner,
                method="aput",
            )
            raise

    async def _logged_aput_writes(config: RunnableConfig, *args: Any, **kwargs: Any) -> Any:
        owner = write_agent_id(config)
        try:
            return await orig_aput_writes(config, *args, **kwargs)
        except asyncio.CancelledError:
            raise  # cancellation is control flow, not a failed write (task #4964)
        except BaseException:
            logger.opt(exception=True).error(
                "checkpoint aput_writes failed",
                event="checkpoint_write_failed",
                agent_id=owner,
                method="aput_writes",
            )
            raise

    checkpointer.aput = _logged_aput  # type: ignore[method-assign]
    checkpointer.aput_writes = _logged_aput_writes  # type: ignore[method-assign]


@dataclass
class _NstepCheckpointState:
    """One thread's retained checkpoint parent, skipped tail, and delta channels."""

    last_aput_step: int | None = None
    last_persisted_config: RunnableConfig | None = None
    last_skipped_aput: (
        tuple[RunnableConfig, Checkpoint, CheckpointMetadata, ChannelVersions] | None
    ) = None
    delta_channels: set[str] = field(default_factory=set[str])


def delta_evidence(checkpoint: Checkpoint, metadata: CheckpointMetadata) -> set[str]:
    """Delta channel names this checkpoint reveals, from two independent signals.

    Both are only ever produced for `DeltaChannel` channels, so a non-empty
    result proves the thread's state uses the delta write model:

    - `counters_since_delta_snapshot` metadata — LangGraph counts delta
      channels only; its keys are the channel names.
    - `_DeltaSnapshot` values in `channel_values` — a delta channel's periodic
      full snapshot. The counters are absent exactly on the super-step that
      took the snapshot (they reset to zero and drop off the metadata), so the
      marker is what keeps detection total.

    Detection is sticky: once any signal is seen, the thread stays delta for
    the saver's lifetime (`_NstepCheckpointState.delta_channels`).
    """
    evidence: set[str] = set()
    counters = metadata.get("counters_since_delta_snapshot")
    if counters:
        evidence.update(counters)
    for name, value in checkpoint.get("channel_values", {}).items():
        if isinstance(value, _DeltaSnapshot):
            evidence.add(name)
    return evidence


def _delta_retires_throttle(
    state: _NstepCheckpointState, checkpoint: Checkpoint, metadata: CheckpointMetadata
) -> bool:
    """Learn the thread's delta channels; report whether the throttle is retired."""
    state.delta_channels.update(delta_evidence(checkpoint, metadata))
    return bool(state.delta_channels)


def _checkpoint_thread_id(config: RunnableConfig) -> str:
    """Return the required LangGraph checkpoint thread id."""
    if "configurable" not in config:
        raise KeyError("checkpoint config is missing configurable")
    configurable = config["configurable"]
    if "thread_id" not in configurable:
        raise KeyError("checkpoint config is missing configurable.thread_id")
    thread_id = configurable["thread_id"]
    if not isinstance(thread_id, str):
        raise TypeError(f"checkpoint thread id must be str, got {type(thread_id).__name__}")
    return thread_id


# The turn's own interval rides the graph's invoke config under this key: LangGraph hands that
# config to every checkpoint write of the run, and the `__` prefix keeps it out of the
# checkpoint metadata the saver derives from the config.
CHECKPOINT_INTERVAL_KEY = "__meiva_checkpoint_interval"


def _resolve_checkpoint_interval(
    interval: int | Callable[[], int], config: RunnableConfig | None = None
) -> int:
    """Return a validated checkpoint interval: the one the write's config carries, else the
    wrapper's default."""
    carried = (config or {}).get("configurable", {}).get(CHECKPOINT_INTERVAL_KEY)
    resolved = carried if carried is not None else interval() if callable(interval) else interval
    if resolved < 1:
        raise ValueError(f"checkpoint interval must be positive, got {resolved}")
    return resolved


def _thread_lock(locks: WeakValueDictionary[str, asyncio.Lock], thread_id: str) -> asyncio.Lock:
    lock = locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[thread_id] = lock
    return lock


def _versions_with_current_blobs(
    new_versions: ChannelVersions, checkpoint: Checkpoint
) -> ChannelVersions:
    """Extend new_versions with every current channel version.

    The saver writes a channel's blob only when that channel appears in
    new_versions. A version born on a skipped super-step gets no blob row, yet
    the next retained checkpoint's channel_versions still reference it; the
    row then dangles and readers that reconstruct channel_values (timeline cold
    load, crash recovery) lose the channel entirely. Merging the full current
    version map makes the retained / final write persist the current value for
    every version it references, keeping the throttle's write reduction while
    every referenced version stays readable by value.

    Delta channels never pass through this merge: a delta-bearing thread is not
    throttled at all (see `wrap_saver_writes_with_nstep_interval`), so every
    version it references is persisted by upstream's own write path. A delta
    channel's non-snapshot steps deliberately have no blob row — readers
    reconstruct those values by replaying `checkpoint_writes` in chain order.
    """
    return {**new_versions, **checkpoint["channel_versions"]}


def wrap_saver_writes_with_nstep_interval(
    checkpointer: AsyncPostgresSaver,
    interval: int | Callable[[], int],
) -> None:
    """Persist super-step checkpoints every ``interval`` steps, except on
    delta-bearing threads.

    A delta-bearing thread retires the throttle entirely: from the first
    checkpoint that reveals a ``DeltaChannel`` (see `delta_evidence`), every
    super-step reaches the saver exactly as upstream writes it — original
    configs, unmerged versions, no skipped writes. A delta channel is
    reconstructed by replaying its ``checkpoint_writes`` in chain order, so a
    skipped batch is unrecoverable and re-homing a batch onto another
    checkpoint scrambles the replay (the fold orders batches by checkpoint
    position only; within one checkpoint it sorts by opaque task-id hash). The
    retirement is the wrapper slice of the delta rebuild
    (`future/infra/data/checkpoint-storage-rebuild.md`): deltas persist every
    super-step, and the throttle's blob merge is a no-op there. A crash on
    such a thread therefore replays at most the in-flight super-step, not
    ``interval - 1`` of them.

    For every other thread the wrapper leaves input/fork checkpoints
    untouched. For skipped super-step checkpoints it skips their channel and
    PUSH writes except for writes at the next retained step. Those writes and
    retained checkpoints use the last persisted config, so every parent and
    write target has a checkpoint row. A completed turn flushes the latest
    skipped update through ``_meiva_nstep_flush(thread_id)``; retained and
    flushed checkpoints persist one full snapshot's blobs per channel, so
    every referenced channel value stays readable; a crash may instead replay
    up to ``interval - 1`` super-steps. A turn names its own interval in its invoke
    config (`CHECKPOINT_INTERVAL_KEY`), so the hosted runner can share one saver
    without sharing a throttle between agents; ``interval`` (a callable is read per
    write) is the default for writes whose config names none.

    The caller installs loud-failure logging first, so these original methods
    are the logging wrappers: every throttled write that fires, including the
    final flush, still reports a checkpoint failure before re-raising. A flush
    forgets its tail only after the save succeeds; failure or cancellation is
    retryable. Writes and flushes serialize per thread, so a concurrent flush
    cannot acknowledge an unfinished save or discard a newly queued checkpoint.
    """
    if isinstance(interval, int) and _resolve_checkpoint_interval(interval) == 1:
        return

    orig_aput, orig_aput_writes = checkpointer.aput, checkpointer.aput_writes
    states: dict[str, _NstepCheckpointState] = {}
    # Flushes and graph writes serialize within one thread. Waiters keep their
    # lock alive; idle thread locks disappear instead of retaining every agent
    # ever seen by a hosted saver. This saver runs on one asyncio event loop.
    locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def _throttled_aput(
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = _checkpoint_thread_id(config)
        async with _thread_lock(locks, thread_id):
            current_interval = _resolve_checkpoint_interval(interval, config)
            if current_interval == 1:
                return await orig_aput(config, checkpoint, metadata, new_versions)

            assert "step" in metadata  # noqa: S101
            assert "source" in metadata  # noqa: S101
            step = metadata["step"]
            source = metadata["source"]
            state = states.setdefault(thread_id, _NstepCheckpointState())
            state.last_aput_step = step
            if _delta_retires_throttle(state, checkpoint, metadata):
                # Delta-bearing thread: the throttle retires — see the wrapper
                # docstring. Pass upstream's own call through untouched; the
                # parent stays the loop's config, versions are not merged, and
                # nothing is buffered for a flush.
                return await orig_aput(config, checkpoint, metadata, new_versions)
            # `loop` is graph.ainvoke's normal super-step path; `update` is the
            # manual state-update path. Both must use the same durability interval.
            if source not in ("loop", "update"):
                saved_config = await orig_aput(
                    state.last_persisted_config or config, checkpoint, metadata, new_versions
                )
                state.last_persisted_config = saved_config
                return saved_config

            # AsyncPregelLoop advances its own checkpoint config without reading
            # aput's return value. Feed every retained checkpoint the last real
            # saver config explicitly, otherwise its parent points at a skipped
            # (and therefore nonexistent) checkpoint row.
            parent_config = state.last_persisted_config or config
            if step % current_interval == 0:
                saved_config = await orig_aput(
                    parent_config,
                    checkpoint,
                    metadata,
                    _versions_with_current_blobs(new_versions, checkpoint),
                )
                state.last_persisted_config = saved_config
                state.last_skipped_aput = None
                return saved_config

            state.last_persisted_config = state.last_persisted_config or config
            state.last_skipped_aput = (config, checkpoint, metadata, new_versions)
            return state.last_persisted_config

    async def _throttled_aput_writes(
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = _checkpoint_thread_id(config)
        async with _thread_lock(locks, thread_id):
            current_interval = _resolve_checkpoint_interval(interval, config)
            if current_interval == 1:
                await orig_aput_writes(config, writes, task_id, task_path)
                return
            state = states.get(thread_id)
            if state is None or state.last_aput_step is None or state.delta_channels:
                # Fail open before the thread's first checkpoint, and retire
                # entirely on a delta-bearing thread: either way the batch keeps
                # its original config — never skipped, never re-homed.
                await orig_aput_writes(config, writes, task_id, task_path)
                return

            write_step = (
                state.last_aput_step
                if any(key == PUSH for key, _value in writes)
                else state.last_aput_step + 1
            )
            if write_step % current_interval == 0:
                await orig_aput_writes(
                    state.last_persisted_config or config, writes, task_id, task_path
                )

    async def _flush_final(thread_id: str) -> None:
        async with _thread_lock(locks, thread_id):
            state = states.get(thread_id)
            if state is not None:
                if state.last_skipped_aput is not None:
                    config, checkpoint, metadata, new_versions = state.last_skipped_aput
                    await orig_aput(
                        state.last_persisted_config or config,
                        checkpoint,
                        metadata,
                        _versions_with_current_blobs(new_versions, checkpoint),
                    )
                # Failure or cancellation leaves the exact tail and parent available
                # for retry. Serialize deletion with graph writes so a newer skipped
                # checkpoint cannot be discarded by this completed flush.
                del states[thread_id]

    checkpointer.aput = _throttled_aput  # type: ignore[method-assign]
    checkpointer.aput_writes = _throttled_aput_writes  # type: ignore[method-assign]
    checkpointer._meiva_nstep_flush = _flush_final  # type: ignore[attr-defined]


async def consume_claimed_inbounds(
    ops_pool: AsyncConnectionPool,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
) -> None:
    """Consume the `'claimed'` chat rows an interrupted turn left behind.

    The host calls it under the admitted incarnation's single-flight on cold
    runtime construction, on database recovery, and at a hosted turn's settled
    end or abort. A claim is consumed whether or not its work reached the
    checkpoint: its result is unknown, and it is never redelivered. The inbound
    owner lock fences concurrent control decisions; the caller must retain that
    identity.
    """
    from agent.db import finalize_claimed_inbounds

    consumed = await finalize_claimed_inbounds(ops_pool, agent_id, incarnation=incarnation)
    if consumed:
        logger.info(
            "consumed {consumed} claimed inbound(s) with unknown results; no redelivery",
            event="inbound_reconcile",
            agent_id=agent_id,
            consumed=consumed,
        )


async def notify_desktop_permissions_at_startup() -> None:
    """Surface broken helper desktop permissions via meiva.ui.notify, once.

    The converge preflight writes independent Screen Recording and
    Accessibility status files. Each is atomically claimed, so concurrent
    starters may independently lose either claim. Unavailable claimed statuses
    share one notice; unreadable and available statuses are stale cleanup. If
    notify fails, every claimed fault is restored for the next startup to retry.

    Must run after SDK init so meiva.ui.notify is registered.
    """
    from base.host.converge.accessibility import AccessibilityStatus
    from base.host.converge.accessibility import (
        status_file_path as accessibility_status_file_path,
    )
    from base.host.converge.screen_capture import ScreenCaptureStatus
    from base.host.converge.screen_capture import (
        status_file_path as screen_capture_status_file_path,
    )

    claimed: list[tuple[Path, Path, ScreenCaptureStatus | AccessibilityStatus]] = []
    for status_path, status_type in (
        (screen_capture_status_file_path(), ScreenCaptureStatus),
        (accessibility_status_file_path(), AccessibilityStatus),
    ):
        processing_path = status_path.with_suffix(".processing")
        # Atomic claim: each concurrent starter can win only this status file.
        try:
            status_path.rename(processing_path)
        except FileNotFoundError:
            continue
        status = status_type.from_file(processing_path)
        if status is None or status.available:
            processing_path.unlink(missing_ok=True)
            continue
        claimed.append((status_path, processing_path, status))

    if not claimed:
        return

    if len(claimed) == 1:
        _, _, status = claimed[0]
        title = status.headline
        content = status.diagnostic
    else:
        _, _, screen_status = claimed[0]
        _, _, accessibility_status = claimed[1]
        title = "Desktop permissions missing"
        content = (
            f"{screen_status.headline}: {screen_status.diagnostic}\n\n"
            f"{accessibility_status.headline}: {accessibility_status.diagnostic}"
        )

    import meiva

    try:
        meiva.ui.notify(  # type: ignore[attr-defined]
            title=title,
            content=content,
            priority="P1",
            idempotency_key=str(uuid4()),
        )
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to post desktop permissions notification",
            event="screen_capture_notify_failed",
        )
        # Restore every claim so the next agent startup retries the notification.
        for status_path, processing_path, _ in claimed:
            processing_path.rename(status_path)
        return
    for _, processing_path, _ in claimed:
        processing_path.unlink(missing_ok=True)
