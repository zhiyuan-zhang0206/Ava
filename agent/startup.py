"""One-shot startup helpers run before the graph loop begins.

- `_wrap_saver_writes_with_loud_failure` — monkey-patch checkpointer aput
  / aput_writes to log every failure as `checkpoint_write_failed` before
  re-raising (LangGraph internally swallows aput failures otherwise)
- `_wrap_saver_writes_with_nstep_interval` — throttle checkpoint writes to
  every Nth super-step while keeping aput_writes in lockstep and exposing a
  final-state flush
- `_reconcile_claimed_inbounds_at_startup` — finalize any 'claimed'
  inbound rows left behind by the previous process of this agent
- `_write_effective_config_to_restart_completed` — write the freshly
  applied config_overlay snapshot into the pending restart_completed row
- `_notify_screen_capture_at_startup` — surface broken OS-level screen capture
  (detected at converge) to the user, exactly once
- `reconcile_open_pages` — probe every open page's server and restore it
  (re-serve dead serve_dir pages, close dead no-dir pages); runs at boot
  and on heartbeat as a catch-all for server death
- `_close_open_page` — CAS open->closed for a dead page row
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.constants import PUSH
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent.hooks.repair import dangling_tool_use_repairs
from shared.log import logger


def _wrap_saver_writes_with_loud_failure(checkpointer: AsyncPostgresSaver, agent_id: int) -> None:
    """Wrap aput / aput_writes to log every failure as a checkpoint_write_failed
    event before re-raising. See call site for the langgraph silent-swallow
    background-task path this defends against.

    The two methods are monkey-patched on the instance — langgraph never
    re-binds them after `__init__`, so this is safe across the saver
    lifetime. Wrapper re-raises to keep langgraph's own retry / propagation
    semantics intact; the only added side effect is a log line per failure.
    """
    orig_aput = checkpointer.aput
    orig_aput_writes = checkpointer.aput_writes

    async def _logged_aput(*args: Any, **kwargs: Any) -> Any:
        try:
            return await orig_aput(*args, **kwargs)
        except BaseException:
            logger.opt(exception=True).error(
                "checkpoint aput failed",
                event="checkpoint_write_failed",
                agent_id=agent_id,
                method="aput",
            )
            raise

    async def _logged_aput_writes(*args: Any, **kwargs: Any) -> Any:
        try:
            return await orig_aput_writes(*args, **kwargs)
        except BaseException:
            logger.opt(exception=True).error(
                "checkpoint aput_writes failed",
                event="checkpoint_write_failed",
                agent_id=agent_id,
                method="aput_writes",
            )
            raise

    checkpointer.aput = _logged_aput  # type: ignore[method-assign]
    checkpointer.aput_writes = _logged_aput_writes  # type: ignore[method-assign]


def _wrap_saver_writes_with_nstep_interval(checkpointer: AsyncPostgresSaver, interval: int) -> None:
    """Persist super-step checkpoints every ``interval`` steps.

    The wrapper leaves input/fork checkpoints untouched. For skipped super-step
    checkpoints it skips their channel and PUSH writes except for writes at the
    next retained step. Those writes and retained checkpoints use the last
    persisted config, so every parent and write target has a checkpoint row. A
    completed turn flushes the latest skipped update through
    ``_ava_nstep_flush``; a crash may instead replay up to ``interval - 1``
    super-steps.

    The caller installs loud-failure logging first, so these original methods
    are the logging wrappers: every throttled write that fires, including the
    final flush, still reports a checkpoint failure before re-raising.
    """
    if interval <= 1:
        return

    orig_aput = checkpointer.aput
    orig_aput_writes = checkpointer.aput_writes
    last_aput_step: int | None = None
    last_persisted_config: RunnableConfig | None = None
    last_skipped_aput: (
        tuple[RunnableConfig, Checkpoint, CheckpointMetadata, ChannelVersions] | None
    ) = None

    async def _throttled_aput(
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        nonlocal last_aput_step, last_persisted_config, last_skipped_aput

        assert "step" in metadata  # noqa: S101
        assert "source" in metadata  # noqa: S101
        step = metadata["step"]
        source = metadata["source"]
        last_aput_step = step
        # `loop` is graph.ainvoke's normal super-step path; `update` is the
        # manual state-update path. Both must use the same durability interval.
        if source not in ("loop", "update"):
            saved_config = await orig_aput(
                last_persisted_config or config, checkpoint, metadata, new_versions
            )
            last_persisted_config = saved_config
            return saved_config

        # AsyncPregelLoop advances its own checkpoint config without reading
        # aput's return value. Feed every retained checkpoint the last real
        # saver config explicitly, otherwise its parent points at a skipped
        # (and therefore nonexistent) checkpoint row.
        parent_config = last_persisted_config or config
        if step % interval == 0:
            saved_config = await orig_aput(parent_config, checkpoint, metadata, new_versions)
            last_persisted_config = saved_config
            last_skipped_aput = None
            return saved_config

        if last_persisted_config is None:
            last_persisted_config = config
        last_skipped_aput = (config, checkpoint, metadata, new_versions)
        return last_persisted_config

    async def _throttled_aput_writes(
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if last_aput_step is None:
            await orig_aput_writes(config, writes, task_id, task_path)
            return

        write_step = (
            last_aput_step if any(key == PUSH for key, _value in writes) else last_aput_step + 1
        )
        if write_step % interval == 0:
            await orig_aput_writes(last_persisted_config or config, writes, task_id, task_path)

    async def _flush_final() -> None:
        nonlocal last_persisted_config, last_skipped_aput

        if last_skipped_aput is None:
            return
        config, checkpoint, metadata, new_versions = last_skipped_aput
        last_persisted_config = await orig_aput(
            last_persisted_config or config, checkpoint, metadata, new_versions
        )
        last_skipped_aput = None

    checkpointer.aput = _throttled_aput  # type: ignore[method-assign]
    checkpointer.aput_writes = _throttled_aput_writes  # type: ignore[method-assign]
    checkpointer._ava_nstep_flush = _flush_final  # type: ignore[attr-defined]


async def _reconcile_claimed_inbounds_at_startup(
    ops_pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    agent_id: int,
) -> None:
    """Load the agent's LangGraph checkpoint, harvest every committed
    `ava_inbound_id` from state.messages, and hand the set to
    `reconcile_claimed_inbounds` so it can finalize any `'claimed'` rows
    left behind by the previous process.

    A fresh process is the only legitimate caller; the agent is bound
    1:1 to a process, so no other writer is concurrently mutating
    `inbound_messages` for this agent_id right now.

    No prior checkpoint (brand-new agent) → no commits to confirm; reconcile
    is still called with an empty set so any unlikely stray `'claimed'`
    rows (e.g. from a previous run that we shouldn't have lost track of)
    are reset to `'pending'`.
    """
    from agent.db import reconcile_claimed_inbounds

    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    ckpt = await checkpointer.aget(config)
    messages = (ckpt or {}).get("channel_values", {}).get("messages", [])
    committed_inbound_ids: set[int] = set()
    for msg in messages:
        ava_id = (getattr(msg, "additional_kwargs", None) or {}).get("ava_inbound_id")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(ava_id, int):
            committed_inbound_ids.add(ava_id)

    committed, reset, dead_lettered = await reconcile_claimed_inbounds(
        ops_pool, agent_id, committed_inbound_ids
    )
    if committed or reset or dead_lettered:
        logger.info(
            "inbound reconcile: {committed} committed → done, {reset} orphans → pending, "
            "{dead_lettered} stale → dead-lettered",
            event="inbound_reconcile",
            agent_id=agent_id,
            committed=committed,
            reset=reset,
            dead_lettered=dead_lettered,
        )


async def _repair_dangling_tool_use_at_startup(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    agent_id: int,
) -> None:
    """Repair dangling tool_use left by a hard-cancelled previous process,
    as crash recovery, before the graph loop begins.

    Runs before graph.ainvoke — i.e. before the claim node can feed the
    history to an LLM (a pending compact_request's summarization call) or
    append inbounds behind the dangling tail. Scans the whole committed
    history, not just the tail: an earlier boot can have buried a dangling
    tool_use mid-history when the consolidating checkpoint `aput` failed at
    shutdown and the dangling AIMessage rode in as a pending write after this
    repair already ran (agents 236/238, 2026-07-13). That pending-write shape
    itself is invisible to `aget_state` here; the before_llm hook twin
    (`agent/hooks/repair.py`) covers it once the graph materializes the write.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    repairs = dangling_tool_use_repairs(snapshot.values.get("messages", []))
    if not repairs:
        return
    await graph.aupdate_state(config, {"messages": repairs})
    checkpointer = cast(AsyncPostgresSaver, graph.checkpointer)  # pyright: ignore[reportUnknownMemberType]
    flush = getattr(checkpointer, "_ava_nstep_flush", None)
    if flush is not None:
        await cast(Callable[[], Awaitable[None]], flush)()
    logger.warning(
        "repaired dangling tool_use(s) from a hard-cancelled previous process",
        event="dangling_tool_use_repaired",
        agent_id=agent_id,
    )


def _write_effective_config_to_restart_completed(agent_id: int) -> None:
    """On new process boot, write effective config snapshot into the most
    recent pending restart_completed inbound's payload.effective_config (PR-E).

    When respawn_agent INSERTs restart_completed, it only passes through
    source/content + the original restart's payload (containing
    config_overlay); this function adds the effective_config snapshot into
    the same row after the new process completes overlay apply — the event
    trail records "overlay input + actually effective config" in full.

    Boot paths other than respawn_agent (spawn / resurrect) have no pending
    restart_completed row; UPDATE rowcount=0 doesn't raise (noop).
    """
    from shared.plugin_config_registry import effective_config_snapshot

    snapshot = effective_config_snapshot()
    import json as _json

    import psycopg as _pg

    from shared.config import settings as _cfg

    with _pg.connect(_cfg.data_plane.db_url) as conn, conn.cursor() as cur:
        # Directly UPDATE the most recent restart_completed (even if
        # status='done', let claim see the latest snapshot). jsonb_set uses
        # path to replace/append the key.
        cur.execute(
            """
            UPDATE inbound_messages
               SET payload = jsonb_set(
                       COALESCE(payload, '{}'::jsonb),
                       '{effective_config}',
                       %s::jsonb,
                       true
                   )
             WHERE id = (
                 SELECT id FROM inbound_messages
                  WHERE agent_id = %s AND kind = 'restart_completed'
                  ORDER BY id DESC LIMIT 1
             )
            """,
            (_json.dumps(snapshot), agent_id),
        )
        conn.commit()


async def _notify_screen_capture_at_startup() -> None:
    """Surface broken OS-level screen capture (detected by the converge
    preflight) to the user via ava.ui.notify, exactly once.

    The status carries which of the two faults it is — the permissions helper holds
    no Screen Recording grant, or the helper never answered so the grant is
    unknown — along with the matching fix, so this only has to render it.

    Notifies at most once even when several agents start concurrently: the
    status file is claimed with an atomic rename, so exactly one starter reads
    it and fires the notice while the others find nothing and skip. On a failed
    notify (e.g. the gateway is unreachable) the file is restored so the next
    startup retries instead of dropping the notice. The next `ava start`
    converge pass rewrites the file if the condition persists.

    Must run after SDK init so ava.ui.notify is registered.
    """
    from shared.screen_capture import ScreenCaptureStatus, status_file_path

    status_path = status_file_path()
    processing_path = status_path.with_suffix(".processing")
    # Atomic claim: only one of several concurrent agents wins the
    # rename. The losers raise FileNotFoundError (source already moved) and skip.
    try:
        status_path.rename(processing_path)
    except FileNotFoundError:
        return
    status = ScreenCaptureStatus.from_file(processing_path)
    if status is None or status.available:
        # Available (or unreadable) status is just stale cleanup — drop it.
        processing_path.unlink(missing_ok=True)
        return

    import ava

    try:
        ava.ui.notify(  # type: ignore[attr-defined]
            title=status.headline,
            content=status.diagnostic,
            priority="P1",
        )
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to post screen capture notification",
            event="screen_capture_notify_failed",
        )
        # Restore the claim so the next agent startup retries the notification.
        processing_path.rename(status_path)
        return
    processing_path.unlink(missing_ok=True)


def _page_server_alive(host: str, port: int) -> bool:
    """Probe a page server's /health endpoint — True when it answers.

    The serve() liveness check answers `ok:<token>`; here any 200 proves the
    server is up (the token only guards against a stale occupant satisfying
    a fresh serve's poll — recovery does not mint a new token, it keeps the
    existing server when it is alive).
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as resp:
            return resp.status == 200
    except OSError:
        return False


async def reconcile_open_pages(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    event_publisher: Any | None = None,
) -> None:
    """Probe every open page's server and restore it — runs at boot and on heartbeat.

    The page-server daemon creates and supervises every serve() page inside a
    daemon-owned persistent shell session for this agent. Those sessions are
    outside rollout service teardown, while the heartbeat probe remains the
    catch-all for server death (crash, OOM, or manual kill): an idle agent
    checks its pages on every heartbeat and self-heals.

    Per open page row:
    - server alive -> keep (log only)
    - server dead + serve_dir set (serve()/serve_markdown()) -> re-serve the
      recorded directory, the old link works again
    - server dead + serve_dir NULL (ava.ui.show() pages, or rows created
      before serve_dir existed) -> the page cannot be rebuilt; close the row
      so the dead link stops showing as open (frontend popover removes the
      entry on the PageClosed event).

    Best-effort: any failure (DB down, probe error, serve error) is logged
    and swallowed — the agent must keep running regardless; the page heals
    on the next heartbeat. `event_publisher` (optional) receives PageClosed
    for rows this pass closes; boot has the publisher, and heartbeat passes
    ctx.event_publisher.
    """
    import asyncio

    rows: list[
        tuple[str, int, str, str | None, str | None]
    ] = []  # name, port, host, title, serve_dir
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT name, port, host, title, serve_dir FROM agent_pages "
                "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL",
                (agent_id,),
            )
            rows = [(r[0], r[1], r[2], r[3], r[4]) for r in await cur.fetchall()]
    except Exception:
        logger.opt(exception=True).warning(
            "page-restore: open-page query failed",
            event="page_restore_query_failed",
            agent_id=agent_id,
        )
        return

    if not rows:
        return

    import ava.ui

    for name, port, host, title, serve_dir in rows:
        try:
            # _page_server_alive is a blocking urllib probe (timeout 1.5s per
            # page) — run it off the event loop so a slow/half-dead page server
            # cannot stall lease renewal / SSE publish / interrupt handling
            # (2026-08-08 audit, cc-backend-runtime P2 — the heartbeat path
            # calls this every 5 minutes).
            if await asyncio.to_thread(_page_server_alive, host, port):
                logger.info(
                    "page-restore: server alive, keeping",
                    event="page_restore_alive",
                    agent_id=agent_id,
                    name=name,
                    port=port,
                )
                continue
            if serve_dir is not None:
                await asyncio.to_thread(ava.ui.serve, serve_dir, name, port, title)
                logger.info(
                    "page-restore: re-served",
                    event="page_restore_reserved",
                    agent_id=agent_id,
                    name=name,
                    port=port,
                )
                continue
            # Dead page with no serve_dir — cannot be rebuilt; close the row
            # so the dead link stops showing as open.
            await _close_open_page(pool, agent_id, name)
            if event_publisher is not None:
                from shared.live_events import PageClosed

                event_publisher.emit(PageClosed(agent_id=agent_id, name=name).model_dump_json())
            logger.warning(
                "page-restore: dead page without serve_dir closed",
                event="page_restore_closed",
                agent_id=agent_id,
                name=name,
                port=port,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "page-restore: reconcile failed",
                event="page_restore_failed",
                agent_id=agent_id,
                name=name,
                port=port,
            )


async def _close_open_page(pool: AsyncConnectionPool, agent_id: int, name: str) -> None:
    """CAS open->closed for one page row (the same UPDATE close_page uses)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agent_pages SET closed_at = now() "
            "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
            "AND expired_at IS NULL",
            (agent_id, name),
        )
