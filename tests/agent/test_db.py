"""Unit tests for agent/db.py + db.py.

Covers agent CRUD + wait_for_inbound Redis pub/sub wake-up + table existence.
No LLM involved.

`shared/db.py` is the UI/kernel shared synchronous helper (`create_agent` / ...), still using
the synchronous `db_conn` fixture. `agent/db.py` is the kernel async path (`wait_for_inbound`
/ `claim_inbound_batch`), using `aops_pool` (AsyncConnectionPool) +
`aredis_inbound_listener` (RedisInboundListener) fixtures — same as production.

Note: claim_inbound_batch is end-to-end covered in tests/agent/test_claim.py via claim_node,
so not repeated here. After Step 1G+ regression, agent/db.py only has two core async functions: wait + claim.

Semantics: compact modifies messages in-place, **does not create a new agent** — hence no
`create_compacted_thread` / `thread_status` / FSM trigger tests.
"""

import asyncio
import time
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import db as agent_db
from agent.db import wait_for_inbound
from shared.config import settings
from shared.db import agent_exists, create_agent, insert_inbound_message, list_agents
from shared.redis_listener import RedisInboundListener
from tests.conftest import spawn_agent

# Redis pub/sub wake-latency discrimination, used by the tests that prove a wake
# rode a real publish, not the defensive SELECT-recheck fallback. `wait_one`
# deliberately does NOT report which path woke it (a SELECT recheck handles both
# uniformly), so latency is the only available signal. We therefore make the gap
# unmistakable rather than chase a tight bound: a real publish wake is sub-second,
# but a loaded CI runner has measured up to ~2.5s (asyncio scheduling + pub/sub
# propagation). The ceiling sits far above that jitter and far below the fallback
# timeout, so crossing it means the fallback fired (a lost publish) — not a slow
# scheduler. Bumping the fallback timeout is free for passing runs (a live publish
# returns long before it); it only bounds how long a genuinely-broken path waits.
_FALLBACK_TIMEOUT_S = 20.0
_PUBLISH_WAKE_CEILING_S = 8.0


def _insert_inbound(
    conn: psycopg.Connection,
    agent_id: int,
    content: str,
    kind: str = "chat",
    source: str = "user",
) -> int:
    """chat kind defaults to source='user' (consistent with production); for non-chat kinds tests usually
    don't care about source, and using 'user' also does not affect claim results (claim does not filter by source)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (agent_id, content, kind, source),
        )
        row = cur.fetchone()
        assert row is not None
        pid = row[0]
    conn.commit()
    return pid


class TestCreateThread:
    def test_creates_with_null_label(self, db_conn: psycopg.Connection) -> None:
        """create_agent leaves label NULL — spawn path gets label generated asynchronously by the gateway BackgroundTask;
        non-spawn (evals) paths do not auto-name. Frontend fallback `#N`."""
        t1 = create_agent(db_conn)
        t2 = create_agent(db_conn)
        t3 = create_agent(db_conn)
        # Sequential allocation is the contract; the absolute starting value is
        # not (serial ids are no longer reset to 1 between tests — see conftest
        # `_clean_state`). Assert consecutiveness, not `== (1, 2, 3)`.
        assert (t2, t3) == (t1 + 1, t1 + 2)
        rows = list_agents(db_conn)
        assert [r[1] for r in rows] == [None, None, None]


class TestThreadExists:
    def test_nonexistent(self, db_conn: psycopg.Connection) -> None:
        assert agent_exists(db_conn, 999) is False

    def test_existing(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        assert agent_exists(db_conn, tid) is True


class TestFatalProviderReport:
    async def test_walk_uses_born_spawner_before_folded_spawner(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = create_agent(db_conn)
        folded_parent = create_agent(db_conn)
        failed = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta "
                "(id, spawner, born_spawner, status, lease_expires_at) "
                "VALUES (%s, 'user', 'user', 'running', now() + interval '1 hour')",
                (root,),
            )
            cur.execute(
                "INSERT INTO agents_meta "
                "(id, spawner, born_spawner, status) "
                "VALUES (%s, %s, %s, 'terminated')",
                (folded_parent, f"agent:{root}", f"agent:{root}"),
            )
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, born_spawner, status) "
                "VALUES (%s, 'user', %s, 'running')",
                (failed, f"agent:{folded_parent}"),
            )
        db_conn.commit()

        def _no_wake(*_args: object) -> None:
            return None

        monkeypatch.setattr(agent_db, "publish_inbound_wake", _no_wake)

        recipient = await agent_db.enqueue_fatal_provider_report_to_nearest_alive_ancestor(
            aops_pool,
            failed,
            error_class="permanent",
            provider="test-provider",
            status=400,
            reason="test-reason",
            occurred_at=datetime.now(UTC),
        )

        assert recipient == root
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, kind, source FROM inbound_messages WHERE kind = 'system_note'"
            )
            row = cur.fetchone()
        assert row == (root, "system_note", "system")


class TestWaitForInbound:
    """`wait_for_inbound` uses Redis pub/sub event-driven approach — `insert_inbound_message`
    publishes to the agent's channel on every INSERT; wait_for_inbound subscribes first,
    then does a SELECT fallback, avoiding busy polling.

    The new design (Step 2 cleanup) no longer filters by kind — the claim node receives all
    pending kinds and dispatches itself; wait only cares whether agent_id has pending (if
    agent_id is provided)."""

    async def test_half_open_redis_command_cannot_block_select_recheck(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A connected-looking cross-machine socket can still stop responding.

        Regression: an unbounded GETDEL on this shape parked the claim loop
        forever, so the fallback SELECT never saw the inbound inserted while it
        was stuck.
        """

        class _HalfOpenCommandConnection:
            is_connected = True

            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.closed = False

            async def getdel(self, _key: str) -> None:
                await self.release.wait()

            async def aclose(self) -> None:
                self.closed = True

        class _LivePubSub:
            is_connected = True

            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        tid = create_agent(db_conn)
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=tid)
        command = _HalfOpenCommandConnection()
        pubsub = _LivePubSub()
        listener._redis = command  # pyright: ignore[reportAttributeAccessIssue]
        listener._pubsub = pubsub  # pyright: ignore[reportAttributeAccessIssue]
        rebuilt_command = _HalfOpenCommandConnection()
        rebuilt_command.release.set()
        rebuilt_pubsub = _LivePubSub()
        rebuilds = 0

        async def _subscribed() -> _LivePubSub:
            nonlocal rebuilds
            if listener._pubsub is None:
                rebuilds += 1
                listener._redis = rebuilt_command  # pyright: ignore[reportAttributeAccessIssue]
                listener._pubsub = rebuilt_pubsub  # pyright: ignore[reportAttributeAccessIssue]
                return rebuilt_pubsub
            return pubsub

        async def _consume_immediately(*_args: object) -> None:
            return None

        monkeypatch.setattr(listener, "_ensure_subscribed", _subscribed)
        monkeypatch.setattr(listener, "_consume_one", _consume_immediately)

        async def _insert_while_waiting() -> None:
            await asyncio.sleep(0.02)
            _insert_inbound(db_conn, tid, "wake")

        insertion = asyncio.create_task(_insert_while_waiting())
        started = time.monotonic()
        waiter = asyncio.create_task(
            wait_for_inbound(aops_pool, listener, agent_id=tid, timeout_s=0.1)
        )
        done, pending = await asyncio.wait({waiter}, timeout=1.0)
        if pending:
            command.release.set()
            await asyncio.wait({waiter}, timeout=1.0)
            pytest.fail("half-open GETDEL blocked wait_for_inbound past its recheck budget")
        await insertion

        assert waiter in done
        assert time.monotonic() - started < 1.0
        assert command.closed, "GETDEL timeout must discard the half-open command socket"
        assert pubsub.closed, "GETDEL timeout must discard the paired pubsub connection"

        await listener.wait_one(0.01)

        assert rebuilds == 1
        assert listener._redis is rebuilt_command
        assert listener._pubsub is rebuilt_pubsub

    async def test_idling_claim_loop_records_each_wait_round(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
    ) -> None:
        """The process-mode OOB detector needs a progress signal independent of
        leases, which keep renewing even if the idle wait loop has died."""
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, status, last_claim_loop_at) "
                "VALUES (%s, 'idling', NULL) "
                "ON CONFLICT (id) DO UPDATE "
                "SET status = 'idling', last_claim_loop_at = NULL",
                (tid,),
            )
        db_conn.commit()

        _insert_inbound(db_conn, tid, "wake")
        await wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=tid, timeout_s=2.0)

        async with aops_pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT last_claim_loop_at FROM agents_meta WHERE id = %s", (tid,)
                )
            ).fetchone()
        assert row is not None and row[0] is not None

    async def test_wakes_on_chat_inbound(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
    ) -> None:
        """chat inbound already INSERTed before waiting → wait sees the SELECT fallback and returns instantly."""
        tid = create_agent(db_conn)
        _insert_inbound(db_conn, tid, "hi", kind="chat")
        await wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=tid, timeout_s=2.0)

    async def test_wakes_on_compact_summary_inbound(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
    ) -> None:
        """compact_summary inbound also wakes — wait does not filter by kind."""
        tid = create_agent(db_conn)
        _insert_inbound(db_conn, tid, "summary", kind="compact_summary")
        await wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=tid, timeout_s=2.0)

    async def test_cas_failure_names_actual_status(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
    ) -> None:
        """A lost status CAS must name the state the row is ACTUALLY in —
        "which concurrent lifecycle op won" is unrecoverable later without it."""
        import pytest

        from agent.db import mark_agent_status

        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, status) VALUES (%s, 'idling') "
                "ON CONFLICT (id) DO UPDATE SET status = 'idling'",
                (tid,),
            )
        db_conn.commit()
        with pytest.raises(RuntimeError, match="actual status now 'idling'"):
            await mark_agent_status(aops_pool, tid, "idling", expected_from="running")

    async def test_flip_hosted_status_commits_and_softens_a_cas_miss(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
    ) -> None:
        """Hosted turn boundaries write their transition, while a lifecycle
        winner merely rejects the stale compare-and-swap."""
        from uuid import uuid4

        from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime

        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, status) VALUES (%s, 'idling') "
                "ON CONFLICT (id) DO UPDATE SET status = 'idling'",
                (tid,),
            )
        db_conn.commit()

        incarnation = await admit_hosted_runtime(
            aops_pool, tid, "unknown", uuid4(), expected_from="idling"
        )
        assert incarnation is not None
        async with aops_pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            ).fetchone()
        assert row == ("running",)

        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (tid,))
        db_conn.commit()

        assert not await settle_hosted_runtime(aops_pool, incarnation)
        async with aops_pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            ).fetchone()
        assert row == ("terminated",)

    async def test_settle_stale_running_rows_only_settles_local_pidless_rows(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
    ) -> None:
        """A hosted boot settles only its prior pidless running rows."""
        from services.agent_host.host import settle_stale_running_rows

        local_stale = create_agent(db_conn)
        local_pid = create_agent(db_conn)
        foreign_stale = create_agent(db_conn)
        terminated = create_agent(db_conn)
        restarting = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO agents_meta (status, pid, machine, id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "status = EXCLUDED.status, pid = EXCLUDED.pid, machine = EXCLUDED.machine",
                [
                    ("running", None, "this-box", local_stale),
                    ("running", 321, "this-box", local_pid),
                    ("running", None, "other-box", foreign_stale),
                    ("terminated", None, "this-box", terminated),
                    ("restarting", None, "this-box", restarting),
                ],
            )
        db_conn.commit()

        assert await settle_stale_running_rows(aops_pool, "this-box") == [local_stale]
        async with aops_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, status FROM agents_meta WHERE id = ANY(%s) ORDER BY id",
                    ([local_stale, local_pid, foreign_stale, terminated, restarting],),
                )
            ).fetchall()
        statuses = dict(rows)
        assert statuses == {
            local_stale: "idling",
            local_pid: "running",
            foreign_stale: "running",
            terminated: "terminated",
            restarting: "restarting",
        }

    async def test_enter_idling_state_cas_loss_degrades_not_crash(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CAS loss on the RUNNING→IDLING flip (a concurrent terminate
        op moving the row between enter_idling_state's pre-SELECT and its
        UPDATE) used to raise RuntimeError and crash the process
        (Task #688). It must degrade to a warning instead: the wait loop then
        discovers the foreign state on its next claim attempt and exits via
        the normal path."""
        import agent.db as adb

        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
        db_conn.commit()

        async def _cas_lost(pool, agent_id, status, *, expected_from):
            raise RuntimeError("simulated CAS loss")

        monkeypatch.setattr(adb, "mark_agent_status", _cas_lost)  # pyright: ignore[reportUnknownArgumentType]
        # must NOT raise — degraded to a warning
        await adb.enter_idling_state(aops_pool, tid)

    async def test_renew_agent_lease_extends_and_scopes_to_alive(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
    ) -> None:
        """R1 (Task #1021): `renew_agent_lease` re-arms `lease_expires_at` for a
        row in an alive status, and is a no-op once the row leaves the alive set
        (a concurrent restart/terminate — this process is being replaced and
        must not keep renewing a lease for a row that is no longer its own)."""
        from datetime import UTC, datetime, timedelta

        from agent.db import renew_agent_lease

        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, status, lease_expires_at) "
                "VALUES (%s, 'running', %s) "
                "ON CONFLICT (id) DO UPDATE SET status = 'running', lease_expires_at = %s",
                (
                    tid,
                    datetime.now(UTC) + timedelta(seconds=1),
                    datetime.now(UTC) + timedelta(seconds=1),
                ),
            )
        db_conn.commit()

        await renew_agent_lease(aops_pool, tid)

        with db_conn.cursor() as cur:
            cur.execute("SELECT lease_expires_at > now() FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone() == (True,)

        # Row leaves the alive set -> renewal is a no-op (lease stays as-is).
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'restarting', lease_expires_at = NULL WHERE id = %s",
                (tid,),
            )
        db_conn.commit()
        await renew_agent_lease(aops_pool, tid)
        with db_conn.cursor() as cur:
            cur.execute("SELECT lease_expires_at FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone() == (None,)

    async def test_wake_from_idle_logs_duration_and_rounds(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        loguru_records,
    ) -> None:
        """A wake that actually waited logs one '[idle-wake]' line with the
        elapsed time and the number of wait rounds — the breakdown that made a
        live 25.5s-pending incident provable (elapsed vs rounds tells whether a
        publish landed mid-wait or the wake rode the SELECT recheck). The
        immediate-pending fast path (tests above) stays silent.

        A mid-wait publish lands well inside the round budget (t_last <<
        timeout) -> the fast path worked -> INFO, not the degraded WARNING."""
        tid = create_agent(db_conn)
        # The shared fixture listener is bound to the pseudo-agent-0 channel;
        # a real wake needs a listener subscribed to THIS agent's channel.
        listener = RedisInboundListener(settings.data_plane.redis_url, tid)
        try:

            async def insert_later() -> None:
                await asyncio.sleep(0.35)
                other = psycopg.connect(settings.data_plane.db_url)
                try:
                    # insert_inbound_message (NOT the raw _insert_inbound): the
                    # Redis pub/sub wake + #1240 breadcrumb is what makes this
                    # a mid-wait fast-path wake — a bare SQL INSERT would only
                    # ever be found by the SELECT recheck (the degraded path).
                    await asyncio.to_thread(insert_inbound_message, other, tid, "wake", "user")
                finally:
                    other.close()

            insert_task = asyncio.create_task(insert_later())
            # timeout_s=2.0 with the insert at 0.35s lands the publish clearly
            # mid-wait (t_last << budget).
            await wait_for_inbound(aops_pool, listener, agent_id=tid, timeout_s=2.0)
            await insert_task
        finally:
            await listener.close()
        wakes = [r for r in loguru_records if "[idle-wake" in r["message"]]
        assert len(wakes) == 1, f"expected exactly one idle-wake line, got {len(wakes)}"  # pyright: ignore[reportUnknownArgumentType]
        assert "wait rounds" in wakes[0]["message"]
        # A publish landed mid-wait -> the fast path worked -> INFO, not the
        # degraded WARNING.
        assert "degraded" not in wakes[0]["message"]
        assert wakes[0]["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]

    async def test_degraded_wake_logs_debug(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
        loguru_records,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A wake that rode the SELECT recheck — every wait_one ran its FULL
        budget, so no publish ever landed mid-wait — logs at DEBUG with
        'degraded'. That is the incident class (2026-08-02, agent 2476: lost
        pub/sub wake, 30s user-visible stall); the degraded flag stays in the
        message for diagnosis, but the level is never WARNING (user ruling
        2026-08-04: idle wake is normal lifecycle flow, debug/info only)."""
        tid = create_agent(db_conn)

        async def fake_wait_one(*, timeout: float) -> None:
            # Simulate a lost wake: always run the full budget, never return
            # early on a publish. Only the loop's SELECT recheck can find the
            # inbound, so elapsed lands at (or past) a round boundary.
            await asyncio.sleep(timeout)

        monkeypatch.setattr(aredis_inbound_listener, "wait_one", fake_wait_one)

        async def insert_later() -> None:
            await asyncio.sleep(0.25)
            other = psycopg.connect(settings.data_plane.db_url)
            try:
                await asyncio.to_thread(_insert_inbound, other, tid, "wake")
            finally:
                other.close()

        insert_task = asyncio.create_task(insert_later())
        # Each fake wait_one runs its full 2.0s budget, so the inbound can only
        # be found by the SELECT recheck at a round boundary: elapsed lands at
        # (or past) rounds * timeout_s, which is exactly the degraded signal.
        await wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=tid, timeout_s=2.0)
        await insert_task
        wakes = [r for r in loguru_records if "[idle-wake" in r["message"]]
        assert len(wakes) == 1, f"expected exactly one idle-wake line, got {len(wakes)}"  # pyright: ignore[reportUnknownArgumentType]
        assert "degraded" in wakes[0]["message"]
        assert wakes[0]["level"].name == "DEBUG"  # pyright: ignore[reportUnknownMemberType]
        assert "rode the SELECT recheck" in wakes[0]["message"]

    async def test_immediate_pending_does_not_log_idle_wake(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        aredis_inbound_listener: RedisInboundListener,
        loguru_records,
    ) -> None:
        """Pending already present → the first SELECT returns, zero wait
        rounds, no '[idle-wake]' noise."""
        tid = create_agent(db_conn)
        _insert_inbound(db_conn, tid, "hi", kind="chat")
        await wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=tid, timeout_s=2.0)
        assert not any("[idle-wake" in r["message"] for r in loguru_records)

    async def test_agent_id_filter_ignores_other_agent(
        self,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
    ) -> None:
        """wait_for_inbound(agent_id=X) only returns for pending of X. Another agent's
        inbound goes through insert_inbound_message → publish to other_tid's channel; our
        listener (my_tid channel) does not receive it (redis channel per-agent), wait continues to block;
        only when our agent's inbound publishes to my_tid channel does it wake + SELECT hits.

        Using a local per-agent listener (not the agent_id=0 fixture): wake depends on publish to
        my_tid's channel, the listener must be subscribed to my_tid to receive it."""
        my_tid = create_agent(db_conn)
        other_tid = create_agent(db_conn)
        listener = RedisInboundListener(settings.data_plane.redis_url, my_tid)

        async def insert_unrelated_then_target() -> None:
            # phase 1: other agent's inbound — publish to other_tid channel, our
            # listener (my_tid) doesn't receive it, must not wake
            await asyncio.sleep(0.3)
            c1 = psycopg.connect(settings.data_plane.db_url)
            try:
                await asyncio.to_thread(insert_inbound_message, c1, other_tid, "noise", "user")
            finally:
                c1.close()
            # phase 2: our agent's inbound — publish to my_tid channel, should wake
            await asyncio.sleep(0.3)
            c2 = psycopg.connect(settings.data_plane.db_url)
            try:
                await asyncio.to_thread(insert_inbound_message, c2, my_tid, "real", "user")
            finally:
                c2.close()

        task = asyncio.create_task(insert_unrelated_then_target())
        t0 = time.monotonic()
        try:
            await wait_for_inbound(
                aops_pool, listener, agent_id=my_tid, timeout_s=_FALLBACK_TIMEOUT_S
            )
        finally:
            await task
            await listener.close()
        elapsed = time.monotonic() - t0
        # must at least wait until phase 2 (0.6s+) before returning; if isolation/filtering is broken,
        # it would return incorrectly in < 0.5s
        assert elapsed >= 0.5, f"wait returned in {elapsed:.2f}s — agent isolation is broken"
        assert elapsed < _PUBLISH_WAKE_CEILING_S, (
            f"wait took {elapsed:.2f}s — phase 2 publish did not wake"
        )


def test_inbound_messages_table_exists(db_conn):
    """After schema rename, the inbound_messages table is available."""
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute("INSERT INTO agents (label) VALUES ('t1') RETURNING id")  # pyright: ignore[reportUnknownMemberType]
        agent_id = cur.fetchone()[0]  # pyright: ignore[reportUnknownMemberType]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'hi', 'chat', 'kernel') RETURNING id",
            (agent_id,),
        )
        assert cur.fetchone()[0] is not None  # pyright: ignore[reportUnknownMemberType]
    db_conn.commit()  # pyright: ignore[reportUnknownMemberType]


class TestReconcileClaimedInbounds:
    """Startup reconciliation closes the gap between agent process death and
    LangGraph commit confirmation: claim marks 'claimed', but only the next
    process's startup can flip 'claimed' → 'done' (commit confirmed via
    ava_inbound_id in state.messages) or 'claimed' → 'pending' (commit lost,
    re-deliver). See agent/db.py:reconcile_claimed_inbounds (agent 57
    incident)."""

    async def test_committed_rows_become_done(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        # Manually create a 'claimed' row to mirror what the previous
        # process's claim_inbound_batch would have left behind
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'committed msg', 'chat', 'user', 'claimed') RETURNING id",
                (tid,),
            )
            iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids={iid}
        )
        assert (committed, reset, dead_lettered) == (1, 0, 0)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))  # pyright: ignore[reportUnknownArgumentType]
            assert cur.fetchone() == ("done",)

    async def test_orphan_rows_reset_to_pending(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'lost msg', 'chat', 'user', 'claimed') RETURNING id",
                (tid,),
            )
            iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        # Empty committed set = simulating "no prior checkpoint" or "this id
        # wasn't in state.messages" — orphan path. Either way it gets reset.
        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids=set()
        )
        assert (committed, reset, dead_lettered) == (0, 1, 0)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))  # pyright: ignore[reportUnknownArgumentType]
            assert cur.fetchone() == ("pending",)

    async def test_mixed_claimed_rows_split_correctly(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Two claimed rows; one in committed_set, one not — first → done,
        second → pending."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'a', 'chat', 'user', 'claimed') RETURNING id",
                (tid,),
            )
            iid_a = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'b', 'chat', 'user', 'claimed') RETURNING id",
                (tid,),
            )
            iid_b = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids={iid_a}
        )
        assert (committed, reset, dead_lettered) == (1, 1, 0)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id IN (%s, %s) ORDER BY id",
                (iid_a, iid_b),  # pyright: ignore[reportUnknownArgumentType]
            )
            rows = dict(cur.fetchall())
        assert rows == {iid_a: "done", iid_b: "pending"}

    async def test_no_claimed_rows_is_noop(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Common boot path: no prior process left claimed rows behind."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids={42, 43}
        )
        assert (committed, reset, dead_lettered) == (0, 0, 0)

    async def test_scope_limited_to_agent_id(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Claimed row for another agent must not be touched — each process
        only reconciles its own agent_id."""
        from agent.db import reconcile_claimed_inbounds

        mine = spawn_agent()
        other = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'theirs', 'chat', 'user', 'claimed') RETURNING id",
                (other,),
            )
            other_iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, mine, committed_inbound_ids=set()
        )
        assert (committed, reset, dead_lettered) == (0, 0, 0)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (other_iid,))  # pyright: ignore[reportUnknownArgumentType]
            assert cur.fetchone() == ("claimed",)

    async def test_stale_orphan_rows_dead_lettered_not_redelivered(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """A claimed row older than the stale threshold is dead-lettered
        (done), not reset to pending — a resurrect of a long-terminated agent
        must not re-deliver ancient mail as fresh messages (Task #654)."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'ancient', 'chat', 'user', 'claimed', "
                "now() - interval '2 days', now() - interval '2 days') RETURNING id",
                (tid,),
            )
            iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids=set()
        )
        assert (committed, reset, dead_lettered) == (0, 0, 1)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))  # pyright: ignore[reportUnknownArgumentType]
            assert cur.fetchone() == ("done",)

    async def test_fresh_orphan_rows_still_reset_to_pending(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Young claims keep the crash-recovery contract: a message lost in
        transit (crash mid-handling) is re-delivered on the next boot."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'recent', 'chat', 'user', 'claimed', "
                "now() - interval '60 seconds', now() - interval '2 minutes') RETURNING id",
                (tid,),
            )
            iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids=set()
        )
        assert (committed, reset, dead_lettered) == (0, 1, 0)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))  # pyright: ignore[reportUnknownArgumentType]
            assert cur.fetchone() == ("pending",)

    async def test_stale_uncommitted_row_dead_lettered_beside_committed(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Committed set wins regardless of age; among the uncommitted, only
        stale rows dead-letter — young ones still reset to pending."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'committed', 'chat', 'user', 'claimed', "
                "now() - interval '3 days', now() - interval '3 days') RETURNING id",
                (tid,),
            )
            committed_iid = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'stale orphan', 'chat', 'user', 'claimed', "
                "now() - interval '2 days', now() - interval '2 days') RETURNING id",
                (tid,),
            )
            stale_iid = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'young orphan', 'chat', 'user', 'claimed', "
                "now() - interval '60 seconds', now() - interval '2 minutes') RETURNING id",
                (tid,),
            )
            young_iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids={committed_iid}
        )
        assert (committed, reset, dead_lettered) == (1, 1, 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id = ANY(%s) ORDER BY id",
                ([committed_iid, stale_iid, young_iid],),
            )
            assert dict(cur.fetchall()) == {
                committed_iid: "done",
                stale_iid: "done",
                young_iid: "pending",
            }

    async def test_null_claimed_at_stale_by_created_at(
        self, db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
    ) -> None:
        """Rows claimed before the claimed_at column existed (2026-08-02)
        carry NULL claimed_at; created_at is their only age evidence and is
        stale by now — dead-letter them instead of re-delivering."""
        from agent.db import reconcile_claimed_inbounds

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'pre-column', 'chat', 'user', 'claimed', NULL, "
                "now() - interval '10 days') RETURNING id",
                (tid,),
            )
            old_iid = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, 'null-but-fresh', 'chat', 'user', 'claimed', NULL, now()) RETURNING id",
                (tid,),
            )
            fresh_iid = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        committed, reset, dead_lettered = await reconcile_claimed_inbounds(
            aops_pool, tid, committed_inbound_ids=set()
        )
        assert (committed, reset, dead_lettered) == (0, 1, 1)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id IN (%s, %s) ORDER BY id",
                (old_iid, fresh_iid),  # pyright: ignore[reportUnknownArgumentType]
            )
            assert dict(cur.fetchall()) == {old_iid: "done", fresh_iid: "pending"}


def test_claimed_inbound_from_row_keeps_created_at_and_claimed_at():
    """from_row must carry the RETURNING created_at + claimed_at (7th/8th
    columns) through to the ClaimedInbound — created_at is stamped onto the
    inbound HumanMessage so the timeline renders the message's real time, not
    a synthetic anchor; claimed_at is the pickup-latency half of delivery
    observability."""
    from datetime import UTC, datetime

    from agent.db import ClaimedInbound

    dt = datetime(2026, 6, 19, 15, 30, tzinfo=UTC)
    claimed = datetime(2026, 6, 19, 15, 30, 5, tzinfo=UTC)
    ci = ClaimedInbound.from_row((7, 1, "hi", "chat", "user", None, dt, claimed))
    assert ci.id == 7
    assert ci.created_at == dt
    assert ci.claimed_at == claimed
