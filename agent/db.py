"""Kernel-specific Postgres operations (async).

LLM history is persisted by LangGraph AsyncPostgresSaver — this module only
handles the kernel-side inbound queue. Cross UI / kernel shared basic agent
CRUD is in `shared/db.py` (sync API). The early process bootstrap claims its
row as `running`; this module then runs kernel-side SQL through
`AsyncConnectionPool`.

Current inbound_messages kinds (see db/schema.sql — this table is the agent's
unified gateway: every trigger entering an agent goes through it):
    chat               — chat message from user / peer agent
    compact_summary    — written by agent ava.self.compact(summary); claim picks it up and directly replaces messages
    compact_request    — UI "/compact" triggers; claim picks it up and runs backend LLM to generate summary replacement
    heartbeat          — heartbeat daemon check-in for idle agents; claim appends system note (tag=NoteTag.HEARTBEAT)
    cancel             — pause; claim discards in-flight LLM generation and routes the agent back to idle (no lifecycle marker)
    terminate          — ava.self.terminate() / admin terminate; claim appends lifecycle marker + goto END
                         (vetoed — consumed no-op, agent stays alive — when a newer/unseen message waits;
                         see has_pending_inbound_after + the veto block in agent/graph/_claim.py)
    restart            — ava.self.restart() / admin restart; claim only marks RESTARTING + goto END (does not append message)
    restart_completed  — respawn_agent INSERT; new process starts, claim appends "You have been restarted by X" marker
    resurrect          — resurrect_agent INSERT; new process starts, claim appends "You have been resurrected by X" marker
    fork               — spawn_agent INSERT on a fork; new process starts, claim appends "you are now agent N, forked from agent:M" identity marker

### Connection model: shared pool + dedicated listener

Two long-lived handles, both built once at process start (`agent/loop.py`):

- `db_pool: AsyncConnectionPool` — a single pool (max_size=2) shared by
  langgraph checkpoint SQL and kernel ad-hoc SQL (CRUD on
  `inbound_messages` / `agents_meta`). The saver opens its cursors with
  `row_factory=dict_row` itself, so the pool keeps psycopg's default
  tuple rows for the positional reads in this module. Together with the
  listener, each agent holds 2 Postgres connections (listener uses Redis
  pub/sub, not PG), so fleet size times two is the Postgres connection
  demand PgBouncer has to absorb (`cli/commands/_pgbouncer.py`).

  The pool's `check_connection` health-checks every borrowed conn with
  SELECT 1 and transparently reconnects when remote PgBouncer / Postgres
  idle-times an underlying connection. Without the pool, a single
  long-lived `AsyncConnection` silently dies after server-side idle
  timeout and every subsequent SQL fails — agent 57 lost 5h of
  checkpoints exactly this way (2026-05-27).

- `RedisInboundListener` — holds a dedicated Redis pub/sub subscription
  to `<prefix>:inbound:{agent_id}` (`shared.cluster.inbound_channel`).
  Redis pub/sub needs its own connection for `get_message()`, so it cannot
  share the pool. The listener auto-reconnects + re-subscribes transparently
  after transient disconnects.

### Wake: Redis pub/sub

`insert_inbound_message` (`shared/db.py`) publishes to the cluster-scoped
Redis channel `<prefix>:inbound:{agent_id}` (`shared.cluster.inbound_channel`)
on every inbound INSERT. The claim node uses `listener.wait_one(timeout=...)`
to block until a publish arrives.

- only the INSERT publishes — mark-done UPDATEs need no wake
- the `timeout` argument is a defensive message-loss recheck (Redis pub/sub
  is fire-and-forget); on expiry, caller does one SELECT to catch up.
  Default 30s.
"""

import time
from datetime import datetime
from typing import Any, NamedTuple, TypeVar

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from shared.agents import AgentStatus
from shared.config import settings
from shared.db import InboundRow
from shared.live_announce import publish_agent_updated
from shared.log import logger
from shared.redis_listener import RedisInboundListener
from shared.timing import self_respawn_restarter_grace_s

# wait_for_inbound default max block time — safety net for lost Redis pub/sub
# wakes (connection jitter / Redis restart). On expiry, fallback SELECT once;
# 30s is plenty for claim node.
# env override: `AVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS`.
DEFAULT_WAIT_TIMEOUT_S = settings.agent.db_notify_wait_timeout_seconds

# A successful borrow that took at least this long still gets a WARNING — a
# healthy pg hands a conn back in milliseconds, so seconds means it is under
# load (or reconnecting). Below the acquire timeout, so a slow-but-served
# borrow is visible before it ever escalates to a hard PoolTimeout.
_SLOW_ACQUIRE_WARN_S = 3.0

_CT = TypeVar("_CT", bound=psycopg.AsyncConnection[Any])


class LoggingConnectionPool(AsyncConnectionPool[_CT]):
    """`AsyncConnectionPool` that names a slow or timed-out connection borrow.

    The pool's `getconn` wait is the agent's only DB backpressure, and its
    default is a silent multi-second block — which is exactly why a transiently
    unresponsive Postgres reads as a mystery node stall rather than a DB
    problem. This override times every borrow: a slow-but-served one logs a
    WARNING, a `PoolTimeout` an ERROR, both tagged with `pool_name`, so a
    degraded pg surfaces in the agent log at the moment it happens. Behaviour is
    otherwise identical to the base pool (the exception still propagates).
    """

    def __init__(self, *args: Any, pool_name: str, **kwargs: Any) -> None:
        self._pool_name = pool_name
        super().__init__(*args, **kwargs)

    async def getconn(self, timeout: float | None = None) -> _CT:
        t0 = time.monotonic()
        try:
            conn = await super().getconn(timeout=timeout)
        except PoolTimeout:
            logger.error(
                "[db pool] {name} acquire timed out after {elapsed:.1f}s "
                "(max_size={mx}) — Postgres unreachable or saturated",
                event="db_pool_acquire_timeout",
                name=self._pool_name,
                elapsed=time.monotonic() - t0,
                mx=self.max_size,
            )
            raise
        elapsed = time.monotonic() - t0
        if elapsed >= _SLOW_ACQUIRE_WARN_S:
            logger.warning(
                "[db pool] {name} acquire took {elapsed:.1f}s (slow — Postgres under load)",
                event="db_pool_acquire_slow",
                name=self._pool_name,
                elapsed=elapsed,
            )
        return conn


class ClaimedInbound(NamedTuple):
    """One inbound row returned by `claim_inbound_batch`.

    `source` is the message channel ('system' / 'user' / 'agent:N' / 'ui:page:<name>' / 'watcher:N' / 'schedule:N' etc.); the
    claim node uses it via `shared/envelope.py:wrap_inbound` to add an envelope
    prefix for kind='chat'; lifecycle kinds directly assemble the marker text
    without going through wrap_inbound.
    """

    id: int
    agent_id: int
    content: str
    kind: str
    source: str
    payload: dict[str, object] | None = None
    created_at: datetime | None = None
    claimed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "ClaimedInbound":
        id_, tid, content, kind, source, payload, created_at, claimed_at = row[:8]
        return cls(id_, tid, content, kind, source, payload, created_at, claimed_at)


async def claim_inbound_batch(
    pool: AsyncConnectionPool,
    agent_id: int,
) -> list[ClaimedInbound]:
    """Atomically grab all pending rows for `agent_id`. Chat kinds flip to
    `'claimed'` (entered the two-phase reconcile path); every other kind
    flips straight to `'done'`. Returns the rows so the claim node can
    dispatch.

    Why kinds differ:

    - **chat** kicks off multi-step LLM+exec processing, with the message
      eventually landing in `state.messages` only after LangGraph saver
      commits — the same long-tail commit window that lost agent 57's
      5h of checkpoints (PR #535). The two-phase path
      (`pending → claimed → done|pending`) plus startup reconcile
      (`reconcile_claimed_inbounds`) auto-recovers from silent commit
      failures.

    - **lifecycle** kinds (terminate / restart / restart_completed /
      resurrect / fork) and **compact** kinds (compact_summary / compact_request)
      cannot be reconciled by scanning `state.messages` for an
      `ava_inbound_id`: `restart` never appends a message at all, and the
      lifecycle / compact messages claim appends carry no inbound id
      (different helper). If they took the two-phase path, the next
      startup's reconcile would see them as orphans and re-deliver — a
      resurrect after a terminate would loop forever on the same terminate
      row, etc. So they keep the legacy one-step semantics: claim marks
      `done` atomically with the grab; a process crash mid-handling means
      the lifecycle effect is lost (matches the pre-2026-05-27 behavior
      the original design accepted as worth the simplicity).

    All kinds claimed together (no kind filter on the SELECT) — dispatch
    happens inside the claim Node, not at the SQL layer. Pool conns are
    configured `autocommit=True`, so the UPDATE commits as soon as
    RETURNING fetches; no explicit `commit()` needed.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        # CASE-on-kind in a single UPDATE keeps the batch grab atomic — chat
        # and non-chat rows in the same batch all commit together. RETURNING
        # order for UPDATE … WHERE id IN (subquery) is heap-scan order, not
        # the subquery's ORDER BY; Python-side sort by created_at re-imposes
        # FIFO.
        await cur.execute(
            "UPDATE inbound_messages SET status = "
            "CASE WHEN kind = 'chat' THEN 'claimed' ELSE 'done' END, "
            "claimed_at = now() "
            "WHERE id IN ("
            "  SELECT id FROM inbound_messages "
            "  WHERE status = 'pending' AND agent_id = %s "
            "  ORDER BY created_at ASC, id ASC "
            "  FOR UPDATE SKIP LOCKED"
            ") RETURNING id, agent_id, content, kind, source, payload, created_at, claimed_at",
            (agent_id,),
        )
        rows = await cur.fetchall()
    rows.sort(key=lambda r: r[6])  # created_at FIFO (index follows SELECT column count)
    return [ClaimedInbound.from_row(r) for r in rows]


async def list_chat_inbound_anchors(pool: AsyncConnectionPool, agent_id: int) -> list[InboundRow]:
    """Read this agent's kind='chat' inbound rows (any status) in created_at
    ascending order — ts anchors for rendering inbound HumanMessages in a
    timeline snapshot.

    These rows commit synchronously when INSERTed / claimed, so reading them
    never races the async checkpoint commit (the reason the timeline render
    moved off a checkpoint re-read).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'chat' ORDER BY created_at ASC",
            (agent_id,),
        )
        rows = await cur.fetchall()
    return [InboundRow(*r) for r in rows]


async def reconcile_claimed_inbounds(
    pool: AsyncConnectionPool,
    agent_id: int,
    committed_inbound_ids: set[int],
) -> tuple[int, int, int]:
    """Finalize every `'claimed'` row for `agent_id` based on whether its
    HumanMessage actually made it into `state.messages`.

    Called once on agent process startup. The new process reads its
    LangGraph checkpoint, extracts every `additional_kwargs.ava_inbound_id`
    from `state.messages` (caller-supplied `committed_inbound_ids`), and
    passes that set here. Each `'claimed'` row is then either:

      - **flipped to `'done'`** if its id is in `committed_inbound_ids` —
        the previous process's claim → langgraph commit chain completed
        successfully before exit;
      - **dead-lettered to `'done'`** if it is older than the stale-claimed
        threshold (`settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds`,
        age from `claimed_at`, falling back to `created_at` for pre-column
        rows) — a claim that old is either already answered or hopelessly
        stale, and re-delivering it would surface an ancient message as
        fresh (Task #654: terminated agents pile up claimed rows forever;
        a resurrect with a pruned checkpoint would otherwise reset them all
        to 'pending' and flood the fresh context). The same cutoff the
        delivery watchdog's sweep applies to terminated owners, applied
        here at boot so the resurrect race is closed at the source;
      - **reset to `'pending'`** otherwise — the message was lost in
        transit (process crash mid-handling OR a silent saver-commit
        failure like agent 57). The same inbound will be re-claimed and
        re-delivered to the new process on its first claim cycle.

    Returns `(committed_count, reset_count, dead_lettered_count)` for
    logging. All writes happen in a single transaction; pool conns are
    `autocommit=True` so callers don't need an explicit commit.

    A fresh process is the only legitimate caller — the function assumes
    no other process is concurrently claiming for this agent_id (an agent
    is process-bound 1:1). Calling it mid-run would race with active claim
    nodes.
    """
    stale_cutoff_s = settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
    if not committed_inbound_ids:
        # No commits to confirm — every claimed row is an orphan, treat as
        # one reset path. (Common shape: brand-new process, no prior
        # in-flight work; the SELECT below will likely return 0 rows.)
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE inbound_messages SET status = 'done' "
                "WHERE status = 'claimed' AND agent_id = %s "
                "  AND COALESCE(claimed_at, created_at) "
                "      < now() - make_interval(secs => %s)",
                (agent_id, stale_cutoff_s),
            )
            dead_lettered = cur.rowcount
            await cur.execute(
                "UPDATE inbound_messages SET status = 'pending' "
                "WHERE status = 'claimed' AND agent_id = %s",
                (agent_id,),
            )
            return (0, cur.rowcount, dead_lettered)

    # Wrap both updates in an explicit transaction so a crash between them
    # can't leave the table half-finalized (some claimed rows already
    # `done`, the rest still `claimed` waiting on a later boot). Pool conn
    # is autocommit=True; `conn.transaction()` opens an explicit BEGIN.
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND agent_id = %s AND id = ANY(%s)",
            (agent_id, sorted(committed_inbound_ids)),
        )
        committed = cur.rowcount
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND agent_id = %s "
            "  AND COALESCE(claimed_at, created_at) "
            "      < now() - make_interval(secs => %s)",
            (agent_id, stale_cutoff_s),
        )
        dead_lettered = cur.rowcount
        await cur.execute(
            "UPDATE inbound_messages SET status = 'pending' "
            "WHERE status = 'claimed' AND agent_id = %s",
            (agent_id,),
        )
        reset = cur.rowcount
    return (committed, reset, dead_lettered)


async def finalize_claimed_inbounds(pool: AsyncConnectionPool | None, agent_id: int) -> int:
    """Mark every 'claimed' row for `agent_id` as 'done' — the compaction
    terminal-consumption point.

    Companion to `reconcile_claimed_inbounds` for the compact path. A
    compaction REMOVE_ALLs the whole checkpoint history, so any claimed row
    whose HumanMessage lived there loses the `ava_inbound_id` that startup
    reconcile matches on; the next restart would see those rows as orphans,
    reset them to 'pending', and re-deliver already-answered messages — which
    surface to the user as a run of consecutive user messages with the
    (compacted) replies missing (Task #823).

    Compaction is the terminal consumption point for every claimed row: their
    content is folded into the summary (or was folded by an earlier
    compaction), so all of them finalize here. Only the co-batched chats —
    deferred to 'pending' by `_defer_chats_to_pending` before this runs —
    survive for re-delivery into the fresh context.

    Returns the number of rows finalized. Pool conns are `autocommit=True`;
    the UPDATE commits as soon as it executes. Safe on the compact path only:
    the agent is process-bound 1:1 and mid-turn, so no other writer is
    concurrently claiming for this agent_id. ``pool is None`` (container /
    eval mode, which has no inbound queue) is a no-op.
    """
    if pool is None:
        return 0
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE inbound_messages SET status = 'done' "
            "WHERE status = 'claimed' AND agent_id = %s",
            (agent_id,),
        )
        return cur.rowcount


async def _has_pending_inbound(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Whether there is a status='pending' inbound (filtered by agent_id)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages WHERE status = 'pending' AND agent_id = %s LIMIT 1",
            (agent_id,),
        )
        return await cur.fetchone() is not None


async def has_pending_inbound_after(
    pool: AsyncConnectionPool,
    agent_id: int,
    after_id: int,
) -> bool:
    """Whether any status='pending' inbound for `agent_id` has id > `after_id`.

    The claim node's terminate veto uses this as a final queue recheck: after
    claiming a batch, a pending row newer than the whole batch means the world
    moved while the exit was being decided — the terminate must yield so the
    fresh message gets processed instead of being stranded by the process death
    (see the veto block in `agent/graph/_claim.py` claim_node routing).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE status = 'pending' AND agent_id = %s AND id > %s LIMIT 1",
            (agent_id, after_id),
        )
        return await cur.fetchone() is not None


async def has_pending_interrupt(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Whether a status='pending' EXTERNAL interrupt (kind cancel/terminate) is queued.

    The in-flight llm/exec node polls this (via `subscribe_interrupt`) to abort
    the current action the moment a pause/terminate lands — without claiming the
    row. The dispatch itself (cancel -> idle, terminate -> END) stays in the
    claim node, the single owner of inbound semantics.

    Self-initiated lifecycle (`source='self'`, i.e. `ava.self.terminate()` /
    restart from inside the agent's own exec) is EXCLUDED: that path already
    raises `_LifecycleExit` inside the exec child, which the exec node handles
    directly, so the watcher must not also fire on the agent's own row — doing
    so would race the clean lifecycle exit with an external cancel. Only
    external interrupts (user / admin / peer) need
    the in-flight abort; the self row is still dispatched normally at claim.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE status = 'pending' AND agent_id = %s AND kind IN ('cancel', 'terminate') "
            "AND source <> 'self' "
            "LIMIT 1",
            (agent_id,),
        )
        return await cur.fetchone() is not None


async def mark_agent_status(
    pool: AsyncConnectionPool,
    agent_id: int,
    status: str,
    *,
    expected_from: str,
) -> None:
    """`UPDATE agents_meta SET status=%s WHERE id=%s AND status=%s` (expected from→to transition).

    Only modifies if the from status matches — 0 rows raises. The claim node
    uses this to flip running ↔ idling. `expected_from` forces the caller to
    think through the transition path, avoiding "blind overwrite" obscuring the
    state machine; the schema CHECK constraint additionally rejects illegal status values.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET status = %s WHERE id = %s AND status = %s",
            (status, agent_id, expected_from),
        )
        if cur.rowcount != 1:
            # Name the state the row is ACTUALLY in — a lost CAS here is almost
            # always a concurrent lifecycle op (terminate / restart / reaper),
            # and "which one won" is unrecoverable later without this read.
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            actual_row = await cur.fetchone()
            actual = actual_row[0] if actual_row is not None else "<row missing>"
            raise RuntimeError(
                f"agents_meta row {agent_id}: status {expected_from!r} → {status!r} "
                f"UPDATE affected {cur.rowcount} rows; actual status now {actual!r} "
                f"(a concurrent lifecycle op won the race)"
            )
        logger.info(
            "[{label}] {body}",
            label="status-change",
            body=f"{expected_from} -> {status}",
            event="status_change",
            **{"from": expected_from, "to": status},
        )
        from shared.audit_events import insert_event_log_async

        await insert_event_log_async(
            event_type="status_change",
            agent_id=agent_id,
            source="system",
            payload={"from": expected_from, "to": status},
        )
    await publish_agent_updated(pool, agent_id)


async def enter_idling_state(pool: AsyncConnectionPool, agent_id: int) -> None:
    """Transition to idling; no-op if already idling.

    A DB outage during the idle wait can cause ``_wait_for_batch``'s
    best-effort IDLING→RUNNING restore to fail against the same dead DB
    (silently suppressed), leaving the row on 'idling' after the outage
    pause. A naive strict CAS would then find 0 rows and raise, killing a
    process that had already recovered. This idempotent entry prevents that.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = await cur.fetchone()
        if row is not None and row[0] == AgentStatus.IDLING:
            return  # already idling — no-op (post-DB-outage re-entry)
    try:
        await mark_agent_status(
            pool, agent_id, AgentStatus.IDLING, expected_from=AgentStatus.RUNNING
        )
    except RuntimeError:
        # CAS lost between the SELECT and the UPDATE — a concurrent lifecycle
        # op (terminate / reaper) moved the row in the gap. Degrade instead
        # of crashing (Task #688): the next claim attempt sees the foreign
        # state and the process exits via the normal path (terminate inbound
        # → END).
        logger.warning(
            "idle flip CAS lost for agent {agent_id} — concurrent lifecycle "
            "op took the row; continuing (next claim attempt will exit cleanly)",
            event="idle_cas_lost",
            agent_id=agent_id,
        )


async def record_claim_loop_progress(pool: AsyncConnectionPool, agent_id: int) -> None:
    """Stamp one process-mode idling claim-loop round.

    The lease renewer is independent from the claim loop, so a live lease does
    not prove the bounded Redis wait and its fallback SELECT are still making
    progress. This marker is written immediately before every recheck round;
    the wedged controller may then identify a silent idling loop even before an
    inbound arrives. The status guard avoids reviving a marker after a
    lifecycle operation has taken ownership of the row.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET last_claim_loop_at = now() WHERE id = %s AND status = 'idling'",
            (agent_id,),
        )


async def wait_for_inbound(
    pool: AsyncConnectionPool,
    listener: RedisInboundListener,
    agent_id: int | None = None,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> None:
    """Async block until an inbound is available to claim.

    `listener` owns the Redis pub/sub wake connection and auto-reconnects
    on connection loss; this function just orchestrates the recheck loop:

    1. SELECT once — catches INSERTs that landed before the subscription
       took effect (or that arrived during a listener reconnect)
    2. `listener.wait_one(timeout)` blocks until wake or timeout
    3. Loop back to step 1

    The SELECT is still kept as defensive cover for the rare case of a
    lost wake message (Redis pub/sub is fire-and-forget — it does not
    guarantee 100% delivery); `timeout_s` bounds how long that recheck
    can be delayed. Conn-down recovery is handled inside the listener, not
    by leaning on this SELECT as a polling fallback.

    Cancel-friendly: any `task.cancel()` propagates through the underlying
    wait as `CancelledError`.

    Args:
        agent_id: only care about a specific agent's inbound. None means look
            at any status=pending (any agent). Claim node in practice always
            passes its own agent_id.
        timeout_s: how long `wait_one` blocks before doing a defensive
            SELECT recheck. Default 30s.
    """
    started = time.monotonic()
    rounds = 0
    while True:
        if agent_id is not None:
            await record_claim_loop_progress(pool, agent_id)
        if agent_id is not None and await _has_pending_inbound(pool, agent_id):
            if rounds:
                # One line per actual wake-from-idle (the immediate-pending
                # fast path stays silent). elapsed vs rounds tells WHICH path
                # delivered the wake: elapsed well under rounds * timeout_s
                # means a publish (or the #1240 wake-key breadcrumb) landed
                # mid-wait; elapsed at a round boundary means the wake rode
                # the SELECT recheck — the pub/sub fast path is degraded and
                # worth investigating. Still a normal wake (user ruling
                # 2026-08-04: idle wake is never WARNING), so it logs at DEBUG
                # while the healthy wake stays INFO — the degraded flag in the
                # message keeps the two distinguishable.
                elapsed = time.monotonic() - started
                # Degraded = the wake rode the SELECT recheck: the last
                # wait_one ran its FULL budget (elapsed >= rounds * timeout_s)
                # instead of returning early on a publish / wake-key
                # breadcrumb. Strict inequality on the healthy side: a
                # mid-wait publish always returns before the round boundary,
                # so only the timeout path can reach the boundary.
                degraded = elapsed >= rounds * timeout_s
                (logger.debug if degraded else logger.info)(
                    "[{label}] {body}",
                    label="idle-wake",
                    body=(
                        f"{'degraded ' if degraded else ''}pending found after "
                        f"{elapsed:.1f}s ({rounds} wait rounds, timeout {timeout_s:.0f}s)"
                        + (" — pub/sub wake lost; rode the SELECT recheck" if degraded else "")
                    ),
                    event="idle_wake",
                    degraded=degraded,
                    elapsed_s=round(elapsed, 3),
                    rounds=rounds,
                    timeout_s=timeout_s,
                )
            return
        await listener.wait_one(timeout=timeout_s)
        rounds += 1
        if agent_id is None:
            # Caller without agent_id: any NOTIFY (or timeout) counts as wake
            return


async def renew_agent_lease(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    ttl_s: float | None = None,
) -> None:
    """Re-arm the agent's liveness lease (R1, Task #1021): `lease_expires_at`
    = now + TTL. Called by the run loop's renewal task on
    `AGENT_LEASE_RENEW_INTERVAL_S` — the one constant of the process, idle
    included — so a row with an unexpired lease IS a live process, and expiry
    is the crash-reclaim judgment the reaper acts on.

    Scoped to the alive statuses: a concurrent transition to 'restarting' /
    'terminated' (a restart, a terminate, a reap) makes the UPDATE a no-op,
    which is correct — this process is being replaced and must not keep
    renewing a lease for a row that is no longer its own.
    """
    from shared.db import ALIVE_STATUSES
    from shared.deploy_timing import AGENT_LEASE_TTL_S

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE id = %s AND status = ANY(%s)",
            (ttl_s if ttl_s is not None else AGENT_LEASE_TTL_S, agent_id, list(ALIVE_STATUSES)),
        )


def _restarter_claimed_before_deadline(cur: psycopg.Cursor, agent_id: int) -> bool:
    """Poll the authoritative row until the restarter wins or its grace ends."""
    deadline = time.monotonic() + self_respawn_restarter_grace_s()
    while True:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None or row[0] != "restarting":
            logger.debug(
                "[self-respawn] agent %s: restarter claimed restart, skipping self-respawn",
                agent_id,
            )
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(settings.daemon.restarter_poll_interval_seconds, remaining))


def schedule_self_respawn(agent_id: int) -> None:
    """Register an atexit handler that self-respawns this agent if the restarter
    daemon doesn't pick it up within a few seconds.

    This is a **fallback** for self-initiated restarts (``source='self'``)
    when the restarter is paused (during a cluster update rollout) or dead.
    The restarter is the primary mechanism — the atexit handler checks whether
    the row is still ``'restarting'`` and only acts when the restarter has not
    claimed it.

    Race-safe: the handler's ``UPDATE … WHERE status='restarting'`` CAS ensures
    only one winner (restarter or self-respawn) launches the replacement
    process.

    The replacement carries this agent's per-agent config forward
    (``$AVA_AGENT_CONFIG_OVERLAY`` / ``$AVA_AGENT_BIRTH_CONFIG``, read off the
    row in the same connection as the CAS, and set in the child's env — never
    argv, since either may carry a provider api_key and argv is world-readable
    via `ps` (issue #974)). Without that, a restart taken over by THIS path —
    which happens exactly when the restarter is paused, i.e. mid-rollout —
    would silently drop the agent back onto cluster defaults: a different
    model, a different skill set, a different identity, with nothing in the
    trail saying so. The read is best-effort like the CAS itself; if it fails
    the replacement still launches, just without the maps.
    """
    import atexit
    import json
    import os
    import subprocess
    import sys

    from shared.config import settings
    from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
    from shared.log import logger

    python = sys.executable
    # The boot-watchdog windows too, and for the same reason they are on argv at
    # all: the replacement must arm its watchdog before it can import
    # `shared.config`, so the values have to arrive on a channel readable at that
    # point. This is the ONE launch path outside `ops/agent_launch.py`, and a
    # child launched without them arms nothing — quietly returning the launcher's
    # liveness probe to the guess it used to be, with no error anywhere. It
    # matters most precisely here: this path runs when the restarter is paused
    # mid-rollout, which is when boxes are busiest.
    base_argv = [
        python,
        "-m",
        "agent",
        "--agent-id",
        str(agent_id),
        "--boot-stall-seconds",
        str(settings.gateway.agent_boot_stall_seconds),
        "--boot-budget-seconds",
        str(settings.gateway.agent_boot_budget_seconds),
    ]

    # Reproduce the agent's launch environment so the child inherits the same
    # venv / PATH as the current process.
    env = os.environ.copy()

    def _config_env_updates(cur: psycopg.Cursor) -> dict[str, str]:
        """`$AVA_AGENT_CONFIG_OVERLAY` / `$AVA_AGENT_BIRTH_CONFIG` for whichever
        of the two the row carries — the same pair `ops/agent_launch.py` sets
        on every other launch path (child env, never argv)."""
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        row = cur.fetchone()
        if row is None:
            return {}
        updates: dict[str, str] = {}
        for env_name, value in (
            (AGENT_CONFIG_OVERLAY_ENV, row[0]),
            (AGENT_BIRTH_CONFIG_ENV, row[1]),
        ):
            if value:
                updates[env_name] = json.dumps(value, sort_keys=True)
        return updates

    def _respawn() -> None:
        argv = list(base_argv)
        from shared.platform import (
            CREATE_NO_WINDOW,
        )  # used below, outside the try (import must not live in it)

        try:
            import psycopg

            from shared.db import PG_STATEMENT_TIMEOUT_KWARGS

            # Poll before the fallback CAS until a fixed deadline. The restarter
            # owns the preferred path because it records restart_completed; an
            # early CAS would silently lose that lifecycle marker. Autocommit
            # makes every poll observe the restarter's latest status change.
            conn = psycopg.connect(
                settings.data_plane.db_url, autocommit=True, **PG_STATEMENT_TIMEOUT_KWARGS
            )
            try:
                with conn.cursor() as cur:
                    if _restarter_claimed_before_deadline(cur, agent_id):
                        return
                    cur.execute(
                        "UPDATE agents_meta SET status = 'idling', pid = NULL, "
                        "started_at = NULL, lease_expires_at = NULL "
                        "WHERE id = %s AND status = 'restarting'",
                        (agent_id,),
                    )
                    if cur.rowcount == 0:
                        logger.debug(
                            "[self-respawn] agent %s: status no longer 'restarting' — "
                            "restarter won the race, skipping self-respawn",
                            agent_id,
                        )
                        return
                    # Commit is automatic (autocommit=True); the row is now
                    # unclaimed 'idling' — the new process will claim it via the
                    # normal claim_agent_row path. Read the
                    # per-agent config off the same connection so the replacement
                    # keeps this agent's overlay + birth stamp.
                    env.update(_config_env_updates(cur))
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "[self-respawn] agent %s: DB update failed, launching anyway",
                agent_id,
                exc_info=True,
            )

        logger.info(
            "[self-respawn] agent %s: restarter did not claim restart, "
            "launching replacement process",
            agent_id,
        )
        subprocess.Popen(  # noqa: S603 — argv is sys.executable + trusted agent_id (int)
            argv,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    atexit.register(_respawn)
