"""Unit tests for agent/db.py + db.py.

Covers agent CRUD + wait_for_inbound Redis pub/sub wake-up + table existence.
No LLM involved.

`shared/db.py` is the UI/kernel shared synchronous helper (`create_agent` / ...), still using
the synchronous `db_conn` fixture. `agent/db.py` is the kernel async path (`wait_for_inbound`
/ `claim_inbound_batch`), using `aops_pool` (AsyncConnectionPool) +
real PostgreSQL connections, matching the host's transactional queue.

Note: claim_inbound_batch is end-to-end covered in tests/agent/test_claim.py via claim_node,
so not repeated here. After Step 1G+ regression, agent/db.py only has two core async functions: wait + claim.

Semantics: compact modifies messages in-place, **does not create a new agent** — hence no
`create_compacted_thread` / `thread_status` / FSM trigger tests.
"""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import db as agent_db
from shared.db import agent_exists, create_agent, list_agents
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
