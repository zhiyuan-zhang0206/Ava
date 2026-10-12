"""Unit tests for agent/db/__init__.py + db.py.

Covers agent CRUD + wait_for_inbound Redis pub/sub wake-up + table existence.
No LLM involved.

`base/db/__init__.py` is the UI/kernel shared synchronous helper (`create_agent` / ...), still using
the synchronous `db_conn` fixture. `agent/db/__init__.py` is the kernel async path (`wait_for_inbound`
/ `claim_inbound_batch`), using `aops_pool` (AsyncConnectionPool) +
real PostgreSQL connections, matching the host's transactional queue.

Note: claim_inbound_batch is end-to-end covered in agent/tests/claim/test_claim.py via claim_node,
so not repeated here. After Step 1G+ regression, agent/db/__init__.py only has two core async functions: wait + claim.

Semantics: compact modifies messages in-place, **does not create a new agent** — hence no
`create_compacted_thread` / `thread_status` / FSM trigger tests.
"""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import db as agent_db
from agent.db import finalize_claimed_inbounds
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, agent_exists, create_agent, list_agents
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.units import spawn_agent

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
        database: Database,
        event_bus: EventBus,
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

        def _no_wake(_db: object, _bus: object, *_args: object) -> None:
            return None

        monkeypatch.setattr(agent_db, "publish_inbound_wake", _no_wake)

        recipient = await agent_db.enqueue_fatal_provider_report_to_nearest_alive_ancestor(
            aops_pool,
            failed,
            error_class="permanent",
            provider="test-provider",
            vendor="test-vendor",
            status=400,
            reason="test-reason",
            occurred_at=datetime.now(UTC),
            bus=event_bus,
            db=database,
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


class TestFinalizeClaimedInbounds:
    """A claimed chat row left behind by interrupted work is consumed, never
    redelivered: its result is unknown, so the queue's `done` only means
    consumed. See agent/db/__init__.py:finalize_claimed_inbounds (agent 57
    incident)."""

    @staticmethod
    def _insert_claimed(
        db_conn: psycopg.Connection, agent_id: int, content: str, age: str = "60 seconds"
    ) -> int:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, status, claimed_at, created_at) "
                "VALUES (%s, %s, 'chat', 'user', 'claimed', "
                "now() - %s::interval, now() - %s::interval) RETURNING id",
                (agent_id, content, age, age),
            )
            row_id = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()
        return row_id

    @staticmethod
    def _statuses(db_conn: psycopg.Connection, ids: list[int]) -> dict[int, str]:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM inbound_messages WHERE id = ANY(%s) ORDER BY id", (ids,)
            )
            return dict(cur.fetchall())

    async def test_every_claimed_row_is_consumed_without_redelivery(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """Young and ancient claims alike go to done: a message lost in transit
        is not re-delivered, and neither is mail from a long-dead agent."""
        tid = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        young = self._insert_claimed(db_conn, tid, "young", "60 seconds")
        ancient = self._insert_claimed(db_conn, tid, "ancient", "2 days")

        consumed = await finalize_claimed_inbounds(aops_pool, tid, incarnation=None)

        assert consumed == 2
        assert self._statuses(db_conn, [young, ancient]) == {young: "done", ancient: "done"}

    async def test_no_claimed_rows_is_noop(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """Common boot path: no prior process left claimed rows behind."""
        tid = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        assert await finalize_claimed_inbounds(aops_pool, tid, incarnation=None) == 0

    async def test_pending_rows_stay_pending(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """New messages are separate inputs: only claims are consumed."""
        tid = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        claimed = self._insert_claimed(db_conn, tid, "claimed")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'new', 'chat', 'user', 'pending') RETURNING id",
                (tid,),
            )
            pending = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()

        assert await finalize_claimed_inbounds(aops_pool, tid, incarnation=None) == 1
        assert self._statuses(db_conn, [claimed, pending]) == {
            claimed: "done",
            pending: "pending",
        }

    async def test_scope_limited_to_agent_id(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        aops_pool: AsyncConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """Claimed row for another agent must not be touched — each process
        only consumes its own agent_id."""
        mine = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        other = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        other_iid = self._insert_claimed(db_conn, other, "theirs")

        assert await finalize_claimed_inbounds(aops_pool, mine, incarnation=None) == 0
        assert self._statuses(db_conn, [other_iid]) == {other_iid: "claimed"}


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
