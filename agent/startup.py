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
  (re-serve dead serve_dir pages, close dead no-dir pages); runs at boot,
  on heartbeat, and on the periodic `page_reconcile_loop` as a catch-all
  for server death
- `page_reconcile_loop` — the heartbeat-independent periodic scan (every
  heartbeat interval, AVA_HEARTBEAT_INTERVAL_SECONDS): a busy agent's
  pages still heal while it works, even though heartbeats only reach
  idle agents
- `reconcile_all_open_pages` — the hosted daemon's periodic scan (task
  #2260): one pass over every agent with open pages, sharing the same
  per-agent interval throttle
- `_close_dead_show_pages` — close dead no-serve_dir rows in one
  transaction with a re-serve notice to the agent (deduped per 6h)
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.constants import PUSH
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent.hooks.repair import dangling_tool_pairing_repairs
from shared.db_transaction import async_write_transaction
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
    """Repair dangling tool_use/tool_result pairing left by a hard-cancelled
    previous process, before the graph loop begins.

    Runs before graph.ainvoke — i.e. before the claim node can feed the
    history to an LLM (a pending compact_request's summarization call) or
    append inbounds behind the dangling tail. Scans the whole committed
    history, not just the tail: an earlier boot can have buried a dangling
    tool_use mid-history when the consolidating checkpoint `aput` failed at
    shutdown and the dangling AIMessage rode in as a pending write after this
    repair already ran (agents 236/238, 2026-07-13). It also drops a
    tool_result whose carrying tool_use was lost (agent 5333, 2026-08-31).
    Pending-write shapes are invisible to `aget_state` here; the before_llm
    hook twin (`agent/hooks/repair.py`) covers them once the graph materializes
    the write.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    repairs = dangling_tool_pairing_repairs(snapshot.values.get("messages", []))
    if not repairs:
        return
    await graph.aupdate_state(config, {"messages": repairs})
    checkpointer = cast(AsyncPostgresSaver, graph.checkpointer)  # pyright: ignore[reportUnknownMemberType]
    flush = getattr(checkpointer, "_ava_nstep_flush", None)
    if flush is not None:
        await cast(Callable[[], Awaitable[None]], flush)()
    logger.warning(
        "repaired dangling tool_use/tool_result pairing from a hard-cancelled previous process",
        event="dangling_tool_pairing_repaired",
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
        conn.execute("SET TRANSACTION READ WRITE")
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


# time.monotonic() of the last reconcile pass per agent (boot, heartbeat,
# or periodic). The periodic loops — the process-mode `page_reconcile_loop`
# and the hosted daemon scan (task #2260) — skip an agent's pass when
# another path already scanned it within the interval, keeping the combined
# cadence at ~one pass per interval instead of two. Keyed by agent_id
# because the hosted daemon serves MANY agents in one process: a single
# timestamp would let one agent's heartbeat scan suppress every other
# agent's pass. Plain float values (no asyncio.Lock — which would bind to
# one event loop): each agent runs a single loop, tests run one loop per
# case. The no-lock argument assumes a single event loop per process —
# the current shape of both runtimes; revisit if multi-threaded execution
# is ever introduced.
_last_reconcile_at: dict[int, float] = {}


async def reconcile_open_pages(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    event_publisher: Any | None = None,
) -> None:
    """Probe every open page's server and restore it — boot, heartbeat, and periodic.

    The page-server daemon creates and supervises every serve() page inside a
    daemon-owned persistent shell session for this agent. Those sessions are
    outside rollout service teardown, while the heartbeat probe and the
    periodic page-reconcile loop remain the catch-alls for server death
    (crash, OOM, or manual kill): an idle agent checks its pages on every
    heartbeat, and `page_reconcile_loop` covers the busy agent whose
    heartbeats never arrive.

    Per open page row:
    - server alive -> keep (log only)
    - server dead + serve_dir set (serve()/serve_markdown()) -> re-serve the
      recorded directory, the old link works again
    - server dead + serve_dir NULL (ava.ui.show() pages, or rows created
      before serve_dir existed) -> the page cannot be rebuilt; close the row
      so the dead link stops showing as open (frontend popover removes the
      entry on the PageClosed event), and tell the agent to re-serve the
      page with one system inbound — see `_close_dead_show_pages` (task
      #2212: the notice folds into the close path so a boot right after a
      host restart still tells the owner, with no gateway-side scan racing
      the agent's own close).

    Best-effort: any failure (DB down, probe error, serve error) is logged
    and swallowed — the agent must keep running regardless; the page heals
    on the next heartbeat. `event_publisher` (optional) receives PageClosed
    for rows this pass closes; boot has the publisher, and heartbeat passes
    ctx.event_publisher.
    """
    import asyncio

    # This pass counts as this agent's recent scan (boot, heartbeat, or
    # periodic) so the periodic loops can skip their own pass for it. Dict
    # mutation needs no `global` — the name itself is never rebound.
    _last_reconcile_at[agent_id] = time.monotonic()

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

    # Dead show() rows (server dead + no serve_dir) — collected over the
    # probe pass, then closed + notified in ONE transaction below so several
    # dead pages of one agent produce a single notice.
    dead_shows: list[tuple[str, int]] = []  # (name, port)
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
            # Dead page with no serve_dir — cannot be rebuilt; closed (with a
            # re-serve notice) after the probe pass.
            dead_shows.append((name, port))
        except Exception:
            logger.opt(exception=True).warning(
                "page-restore: reconcile failed",
                event="page_restore_failed",
                agent_id=agent_id,
                name=name,
                port=port,
            )

    if dead_shows:
        await _close_dead_show_pages(pool, agent_id, dead_shows, event_publisher)


async def page_reconcile_loop(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    event_publisher: Any | None = None,
    interval_s: float | None = None,
) -> None:
    """Periodically probe + restore open pages — the heartbeat-independent scan.

    The gateway heartbeat only reaches idle agents, so a busy agent's pages
    would otherwise stay unreconciled for the whole turn (task #2257: a
    serve() page died at a platform update and stayed dead ~4h because its
    owner was mid-work and never got a heartbeat). This loop runs every
    `interval_s` — defaulting to the heartbeat interval
    (AVA_HEARTBEAT_INTERVAL_SECONDS, 300 s) so a cluster tuning the
    heartbeat cadence scales the page scan with it — regardless of
    idle/busy; a pass is skipped when another path (boot or heartbeat)
    already reconciled within the interval. The agent's own boot scan
    covers t=0; this loop covers everything after. Self-protecting like
    the lease renewer: any failure is logged and the loop waits for the
    next interval instead of dying silently.
    """
    import asyncio

    from shared.config import settings

    if interval_s is None:
        interval_s = float(settings.daemon.heartbeat_interval_seconds)

    while True:
        await asyncio.sleep(interval_s)
        try:
            if time.monotonic() - _last_reconcile_at.get(agent_id, 0.0) < interval_s:
                continue
            await reconcile_open_pages(pool, agent_id, event_publisher=event_publisher)
        except Exception:
            logger.warning(
                "page-reconcile loop pass failed — retrying next interval",
                event="page_restore_failed",
                agent_id=agent_id,
                exc_info=True,
            )


async def reconcile_all_open_pages(
    pool: AsyncConnectionPool,
    *,
    interval_s: float,
    event_publisher: Any | None = None,
) -> None:
    """Reconcile every open page on this machine — the hosted daemon's scan.

    The hosted turn runner (services/agent_host) serves many agents in one
    process and does not run agent/loop.py:main(), so no per-agent
    `page_reconcile_loop` exists there (task #2260): a busy hosted agent's
    pages would otherwise go unreconciled for as long as its turn lasts —
    the same gap process mode had before #1284. One query lists this
    machine's agents with open pages; each is reconciled through the
    ordinary per-agent pass (which stamps its own throttle key, so a
    heartbeat scan of that agent within `interval_s` suppresses this pass).

    Each pass runs under `bind_turn_identity(agent_id)`: the daemon process
    has no agent identity of its own (no turn context, no AVA_AGENT_ID), and
    the re-serve arm calls ava.ui.serve, whose registration reads
    ava._boot.agent_id() — without the bind it would POST to /agents/None
    and the re-serve would fail silently (P1, #1312 adversarial review).
    asyncio.to_thread copies contextvars, so the probe/serve threads see
    the bind too. Best-effort like the per-agent pass: failures are logged
    per agent and never raise.
    """
    from shared.machine import reachable_host
    from shared.turn_identity import bind_turn_identity

    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT agent_id FROM agent_pages "
                "WHERE host = %s AND closed_at IS NULL AND expired_at IS NULL",
                (reachable_host(),),
            )
            agent_ids = [r[0] for r in await cur.fetchall()]
    except Exception:
        logger.opt(exception=True).warning(
            "page-restore: open-page agent query failed",
            event="page_restore_query_failed",
        )
        return

    for agent_id in agent_ids:
        if time.monotonic() - _last_reconcile_at.get(agent_id, 0.0) < interval_s:
            continue
        try:
            with bind_turn_identity(agent_id):
                await reconcile_open_pages(pool, agent_id, event_publisher=event_publisher)
        except Exception:
            logger.warning(
                "page-restore: per-agent pass failed",
                event="page_restore_failed",
                agent_id=agent_id,
                exc_info=True,
            )


# The re-serve notice prefix — also the dedupe key for the min-interval check.
_PAGE_RECOVERY_NOTICE_PREFIX = "Page recovery:"
# Repeated heartbeats (every 5 min) while a dead row survives must not nag
# the agent; the interval is checked against the agent's own inbound history.
_PAGE_RECOVERY_MIN_INTERVAL_S = 6 * 3600


def _page_recovery_notice(agent_id: int, names: list[str]) -> str:
    """The re-serve notice content; the prefix doubles as the dedupe key."""
    return (
        f"{_PAGE_RECOVERY_NOTICE_PREFIX} page(s) "
        f"{', '.join(repr(n) for n in names)} of agent {agent_id} are no longer "
        "being served (their page server died). Re-serve them with "
        "ava.ui.show() to republish."
    )


async def _recent_page_recovery_notice(cur: Any, agent_id: int) -> bool:
    """Whether this agent was already told within the min interval."""
    cutoff = datetime.now(UTC) - timedelta(seconds=_PAGE_RECOVERY_MIN_INTERVAL_S)
    await cur.execute(
        "SELECT 1 FROM inbound_messages "
        "WHERE agent_id = %s AND source = 'system' "
        "AND content LIKE %s AND created_at > %s LIMIT 1",
        (agent_id, _PAGE_RECOVERY_NOTICE_PREFIX + "%", cutoff),
    )
    return await cur.fetchone() is not None


async def _close_dead_show_pages(
    pool: AsyncConnectionPool,
    agent_id: int,
    dead: Sequence[tuple[str, int]],
    event_publisher: Any | None,
) -> None:
    """Close dead show() rows and tell the agent to re-serve them.

    A show() row (serve_dir NULL) cannot be rebuilt — its page server ran
    inside the agent's own process and died (host restart, crash, manual
    kill). The row is closed so the dead link stops showing as open; the
    agent gets one system-sourced inbound ("Page recovery: ...") listing
    every dead page of this pass, asking it to re-serve them with
    ava.ui.show() (task #2212).

    Close and notice are ONE transaction — a failure rolls back both, so the
    next heartbeat retries the pass and the agent is never told about rows
    that stayed open. The notice is deduped per agent over
    ``_PAGE_RECOVERY_MIN_INTERVAL_S``: the heartbeat runs every 5 minutes,
    so without the window a persistent failure would nag on every pass.
    PageClosed events emit after the commit.
    """
    import asyncio

    names = [name for name, _port in dead]
    notified = False
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            for name in names:
                # CAS open->closed (the same UPDATE close_page uses).
                await cur.execute(
                    "UPDATE agent_pages SET closed_at = now() "
                    "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
                    "AND expired_at IS NULL",
                    (agent_id, name),
                )
            if not await _recent_page_recovery_notice(cur, agent_id):
                await cur.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (%s, %s, 'chat', 'system')",
                    (agent_id, _page_recovery_notice(agent_id, names)),
                )
                notified = True
    except Exception:
        logger.opt(exception=True).warning(
            "page-restore: dead show-page close/notify failed",
            event="page_restore_failed",
            agent_id=agent_id,
            names=names,
        )
        return

    if event_publisher is not None:
        from shared.live_events import PageClosed

        for name in names:
            event_publisher.emit(PageClosed(agent_id=agent_id, name=name).model_dump_json())
    for name, port in dead:
        logger.warning(
            "page-restore: dead page without serve_dir closed",
            event="page_restore_closed",
            agent_id=agent_id,
            name=name,
            port=port,
        )
    if notified:
        # Wake the agent so the notice is claimed promptly (the claim loop's
        # SELECT recheck delivers it within timeout_s regardless). At boot the
        # listener is not subscribed yet — the wake's SETEX breadcrumb makes
        # the listener SELECT immediately on subscribe.
        from shared.db import publish_inbound_wake

        await asyncio.to_thread(publish_inbound_wake, agent_id, "0")
        logger.bind(event="page_restore_notified", agent_id=agent_id).info(
            "page-restore: told agent {agent_id} to re-serve dead show page(s) {names}",
            agent_id=agent_id,
            names=names,
        )
