"""`services.wake.delivery_watchdog.daemon` — stale-pending-inbound selection + alerting.

`select_stale_pending` is the daemon's core predicate: chat inbounds still
`pending` past the threshold. `scan_once` adds the once-per-row-while-stuck
alert semantics (a row that flips pending -> claimed -> pending re-alerts).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.daemon import (
    scan_once,
    select_stale_pending,
)

_THRESHOLD_S = 30.0
_DISPATCH_THRESHOLD_S = 1.0
_MAX_DISPATCH_COUNT = 5
_DISPATCH_BACKOFF_STEPS_S = [5.0, 30.0, 120.0, 300.0]
_HOST_STALENESS_S = 120.0


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentity, unit_home: Path
) -> Iterator[telemetry.EventPipeline]:
    """Own the process writer used by the service operation and mirror barrier."""
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=unit_home
    )
    try:
        telemetry.init_telemetry(
            process="test-wake-service", pipeline=pipeline, machine_reader=machine_identity.name
        )
        yield pipeline
    finally:
        pipeline.stop(timeout=2)


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def idling_agent(
    db: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> int:
    """spawn_agent creates the agents_meta row (create_agent does not — that
    is the spawn path's job); the alert filter reads owner status, so tests
    spawn then park the agent 'idling' (same pattern as the heartbeat daemon
    tests)."""
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _make_running_agent(
    db: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> int:
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
    db.commit()
    return aid


def _insert_old_inbound(
    db: psycopg.Connection, agent_id: int, *, age_s: float, database_gate: ProcessDbGate
) -> int:
    """Insert a chat inbound backdated `age_s` (timestamp-only UPDATE — the
    inbound table has no triggers on created_at). Returns the inbound id."""
    iid = insert_inbound_message(
        db,
        agent_id,
        "stale",
        source="user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (age_s, iid),
        )
    db.commit()  # the pool's connections must see the backdate too
    return iid


def _set_host_verdict(
    db: psycopg.Connection,
    *,
    online: bool = True,
    agent_host: bool = True,
    age_s: float = 0.0,
    machine: str | None = None,
    local_machine: MachineIdentity,
) -> None:
    """Upsert the `machine_probe` verdict the dispatch host gate reads."""

    name = machine or local_machine.name()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_probe (machine_name, online, agent_host_online, "
            "consecutive_failures, last_probe_at) "
            "VALUES (%s, %s, %s, 0, now() - make_interval(secs => %s)) "
            "ON CONFLICT (machine_name) DO UPDATE SET online = EXCLUDED.online, "
            "agent_host_online = EXCLUDED.agent_host_online, "
            "consecutive_failures = EXCLUDED.consecutive_failures, "
            "last_probe_at = EXCLUDED.last_probe_at",
            (name, online, agent_host, age_s),
        )
    db.commit()


@pytest.fixture(autouse=True)
def healthy_host_verdict(db_conn: psycopg.Connection, *, machine_identity: MachineIdentity) -> None:
    """Normal dispatch condition: a fresh reachable machine with a live host.

    Host-gate tests override the verdict inside the test body."""
    _set_host_verdict(db_conn, local_machine=machine_identity)


class TestSelectStalePending:
    def test_returns_only_chat_pending_older_than_threshold(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        old = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        _insert_old_inbound(
            db_conn, aid, age_s=_THRESHOLD_S - 10, database_gate=database_gate
        )  # fresh — excluded
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'terminate', 'system')",
                (aid, "bye"),
            )
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
                "WHERE kind = 'terminate' AND agent_id = %s",
                (_THRESHOLD_S + 5, aid),
            )
        rows = select_stale_pending(pool, _THRESHOLD_S)
        assert [(r[0], r[2]) for r in rows] == [(old, f"#{aid}")] or [r[0] for r in rows] == [old]

    def test_claimed_or_done_rows_never_stale(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_empty_when_nothing_stale(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S - 1, database_gate=database_gate)
        assert select_stale_pending(pool, _THRESHOLD_S) == []

    def test_running_owner_queues_are_not_stalls(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """A chat inbound queued behind a long in-flight turn (owner
        status='running') is normal, not a delivery stall — the turn-end SELECT
        picks it up. Only waiting/terminal owners signal a real stall."""
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (aid,))
        db_conn.commit()
        assert select_stale_pending(pool, _THRESHOLD_S) == []


class TestScanOnce:
    def test_alerts_each_stale_row_once_while_pending(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)

        newly, alerted = scan_once(pool, _THRESHOLD_S, set())
        assert newly == 1
        assert alerted == {iid}

        # Second scan: still stale, already alerted -> no new alerts, no spam.
        newly, alerted = scan_once(pool, _THRESHOLD_S, alerted)
        assert newly == 0
        assert alerted == {iid}

    def test_row_that_leaves_pending_forgets_alert(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        _, alerted = scan_once(pool, _THRESHOLD_S, set())

        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (iid,))
        db_conn.commit()
        _, alerted2 = scan_once(pool, _THRESHOLD_S, alerted)
        assert alerted2 == set()

        # And if it comes back to pending (reconcile reset), it re-alerts.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'pending' WHERE id = %s", (iid,))
        db_conn.commit()
        newly, _ = scan_once(pool, _THRESHOLD_S, alerted2)
        assert newly == 1

    @pytest.mark.usefixtures("event_pipeline")
    def test_alert_writes_unified_event(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """The alert emits through the unified emitter: the canonical
        `events` row (telemetry/delivery_stalled). The legacy agent_events
        mirror is gone (tracker #898 term-alignment)."""
        aid = idling_agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        iid = _insert_old_inbound(db_conn, aid, age_s=_THRESHOLD_S + 5, database_gate=database_gate)
        scan_once(pool, _THRESHOLD_S, set())
        # The emitter drains asynchronously (0.5s cadence) — flush() can race
        # the drain thread for the queue, so poll briefly for the line. The
        # event lives in the JSONL mirror (the PG events copy was retired at
        # the LGTM cutover, task #1197 close-C, and dropped with the archive
        # cleanup, task #1281/#1823).
        import json as _json
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from base.paths import logs_dir

        def _stalled() -> dict[str, object] | None:
            telemetry.flush()
            day = _dt.now(_UTC).strftime("%Y%m%d")
            path = logs_dir() / f"events-{day}.jsonl"
            if not path.exists():
                return None
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    obj = _json.loads(line)
                except ValueError:
                    continue
                if (
                    obj.get("event_name") == "delivery_stalled"
                    and obj.get("agent_id") == aid
                    and obj.get("category") == "telemetry"
                ):
                    return obj
            return None

        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ev = _stalled()
            if ev is not None:
                break
            time.sleep(0.05)
        assert ev is not None
        assert ev["category"] == "telemetry"
        assert ev["event_name"] == "delivery_stalled"
        assert ev["level"] == "warning"
        attributes = ev["attributes"]
        assert isinstance(attributes, dict)
        assert attributes["inbound_id"] == iid
