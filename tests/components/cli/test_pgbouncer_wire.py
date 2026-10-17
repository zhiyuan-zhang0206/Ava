from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Generator
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import MIN_REFRESH_INTERVAL_S, ProcessDbGate
from base.deploy.tests.containers import postgres
from base.deploy.tests.pgbouncer import (
    SECRET,
    admin_console_url,
    pgbouncer_available,
    pgbouncer_in_front,
)
from base.events.live.bus import EventBus
from tests.path_scoped.cli_tests import operator_database as operator_database

pytestmark = pytest.mark.skipif(
    not pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
)


def test_scram_client_auth_and_pooled_select(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, prepare_threshold=None) as conn,
    ):
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1


def test_wrong_secret_is_rejected(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        bad = pooled.replace(SECRET, "not-the-secret")
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(bad, prepare_threshold=None, connect_timeout=5)


def test_transaction_pooling_never_prepare(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as conn,
    ):
        for i in range(50):
            row = conn.execute("SELECT %s::int", (i,)).fetchone()
            assert row is not None and row[0] == i


@contextlib.contextmanager
def _read_only_default_pooler(pg_url: str, pool_size: int = 2) -> Generator[str]:
    with pgbouncer_in_front(pg_url, pool_size=pool_size, read_only_default=True) as pooled:
        with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as probe:
            row = probe.execute("SHOW default_transaction_read_only").fetchone()
            assert row is not None and str(row[0]) == "on"
        yield pooled


def _direct_writer(pg_url: str) -> psycopg.Connection:
    return psycopg.connect(pg_url, options="-c default_transaction_read_only=off")


def _insert_agent(pg_url: str) -> int:
    with _direct_writer(pg_url) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert row is not None
        return int(row[0])


def test_write_transaction_overrides_a_read_only_default_on_connect(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config
    from base.deploy.state import host_deploy_state

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url, pool_size=1) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        host_deploy_state.set_posture(
            Database.from_settings(gate=database_gate), "paused", machine=machine_identity.name()
        )

        with psycopg.connect(pg_url) as verify:
            row = verify.execute("SELECT posture FROM host_deploy_state").fetchone()
        assert row is not None and row[0] == "paused"


def test_schedule_provision_overrides_a_read_only_default(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config
    from cli.commands.management.schedules import cmd_schedules_provision

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        assert cmd_schedules_provision(database_factory=operator_database) == 0

        with psycopg.connect(pg_url) as verify:
            row = verify.execute("SELECT count(*) FROM schedules").fetchone()
        assert row is not None and int(row[0]) > 0


def test_write_transaction_overrides_a_read_only_default_on_pool_borrow(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config
    from base.db import pool
    from services.upkeep.ttl_reaper.shells import delete_shell_row

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        agent_id = _insert_agent(pg_url)
        with _direct_writer(pg_url) as setup:
            setup.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now())",
                (agent_id, 7),
            )
        db_pool = pool(min_size=1, max_size=2, gate=database_gate)
        try:
            delete_shell_row(db_pool, database, event_bus, agent_id, 7, interrupted=False)
        finally:
            db_pool.close()

        with psycopg.connect(pg_url) as verify:
            row = verify.execute(
                "SELECT 1 FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, 7),
            ).fetchone()
        assert row is None


def test_session_touch_overrides_a_read_only_default(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    from gateway.http.auth.session_store import touch_session

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url) as pooled,
    ):
        with _direct_writer(pg_url) as setup:
            setup.execute(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                ("pgbouncer-session-touch",),
            )
        db_pool = ConnectionPool(
            pooled,
            min_size=1,
            max_size=2,
            open=True,
            kwargs={"prepare_threshold": None},
        )
        try:
            touch_session(db_pool, "pgbouncer-session-touch")
        finally:
            db_pool.close()

        with psycopg.connect(pg_url) as verify:
            row = verify.execute(
                "SELECT last_seen_at FROM web_sessions WHERE id = %s",
                ("pgbouncer-session-touch",),
            ).fetchone()
        assert row is not None and row[0] is not None


def test_async_write_transaction_overrides_a_read_only_default(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    from base.db.transaction import async_write_transaction

    async def write_once(pooled: str) -> None:
        db_pool = AsyncConnectionPool(
            pooled,
            min_size=1,
            max_size=2,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
        await db_pool.open()
        try:
            async with async_write_transaction(db_pool) as conn:
                await conn.execute(
                    "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
                )
        finally:
            await db_pool.close()

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url) as pooled,
    ):
        asyncio.run(write_once(pooled))


def test_plain_autocommit_write_still_fails_under_a_read_only_default(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config
    from base.db import connect

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        _read_only_default_pooler(pg_url, pool_size=1) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        with (
            connect(autocommit=True, gate=database_gate) as conn,
            pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
        ):
            conn.execute(
                "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
            )


def test_pooler_isolates_one_clients_read_only_posture_from_another(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url, pool_size=1) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as poisoner,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as other,
    ):
        poisoner.execute("SET default_transaction_read_only = on")
        row = other.execute("SHOW default_transaction_read_only").fetchone()
        assert row is not None and str(row[0]) == "off"
        other.execute(
            "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
        )
        row = poisoner.execute("SHOW default_transaction_read_only").fetchone()
        assert row is not None and str(row[0]) == "on"


def _statement_timeout(conn: psycopg.Connection) -> str:
    row = conn.execute("SHOW statement_timeout").fetchone()
    assert row is not None
    return str(row[0])


def test_connect_query_bounds_pooled_backends_at_birth(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as conn,
    ):
        assert _statement_timeout(conn) == "1min"


def test_base_connect_applies_statement_timeout_on_pooled_dial(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        dp = config.settings.data_plane
        monkeypatch.setattr(dp, "db_url", pooled)

        import base.db

        with base.db.connect(gate=database_gate) as conn:
            assert _statement_timeout(conn) == "1min"


def test_base_pool_applies_statement_timeout_on_pooled_dial(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        dp = config.settings.data_plane
        monkeypatch.setattr(dp, "db_url", pooled)

        import base.db

        pool = base.db.pool(min_size=1, max_size=2, gate=database_gate)
        try:
            with pool.connection() as conn:
                assert _statement_timeout(conn) == "1min"
        finally:
            pool.close()


def test_langgraph_saver_setup_and_roundtrip_through_pgbouncer(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
        PostgresSaver.from_conn_string(pooled) as saver,
    ):
        saver.setup()
        cfg: RunnableConfig = {"configurable": {"thread_id": "pgbouncer-wire", "checkpoint_ns": ""}}
        ckpt = empty_checkpoint()
        saved_cfg = saver.put(cfg, ckpt, {}, {})
        got = saver.get_tuple(saved_cfg)
        assert got is not None
        assert got.checkpoint["id"] == ckpt["id"]


def test_admin_probe_reaches_the_bound_address_only(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    import sys

    from base.cluster.dataplane.pooler import admin_reachable

    if sys.platform == "darwin":
        pytest.skip("127.0.0.2 needs an lo0 alias on macOS")
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url, listen_addr="127.0.0.2") as pooled,
    ):
        listen_port = int(str(conninfo_to_dict(pooled)["port"]))
        assert admin_reachable(listen_port, SECRET, host="127.0.0.2") is True
        assert admin_reachable(listen_port, SECRET, host="127.0.0.1") is False


_POISON_SQL = ("SET search_path = nowhere", "SET statement_timeout = 0")


def _poison_backend(pooled: str) -> None:
    with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as polluter:
        for statement in _POISON_SQL:
            polluter.execute(statement)
    with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as raw:
        row = raw.execute("SHOW search_path").fetchone()
        assert row is not None and str(row[0]) == "nowhere"
        assert _statement_timeout(raw) == "0"


def test_pooled_borrow_scrubs_a_poisoned_backend(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    import base.db as base_db
    from base import config

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url, pool_size=1) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        pool = base_db.pool(min_size=1, max_size=1, gate=database_gate)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _poison_backend(pooled)
            with pool.connection() as conn:
                row = conn.execute("SHOW search_path").fetchone()
                assert row is not None and str(row[0]) != "nowhere"
                assert _statement_timeout(conn) == "1min"
                conn.execute(
                    "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
                )
        finally:
            pool.close()


def test_message_insert_and_schedule_stop_survive_a_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
    publish_wake: Callable[[int, str], bool],
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    import base.db as base_db
    from base import config
    from base.agents.inbox.chat_delivery import insert_chat_inbound_once
    from gateway.schedules.router import update_blocking

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url, pool_size=1) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)

        with psycopg.connect(pg_url, autocommit=True) as admin:
            row = admin.execute(
                "INSERT INTO agents (label) VALUES ('poison-probe-agent') RETURNING id"
            ).fetchone()
            assert row is not None
            agent_id: int = row[0]
            row = admin.execute(
                "INSERT INTO schedules (name, script, command, enabled) "
                "VALUES ('poison-probe-schedule', 'print(1)', 'python schedule.py', true) "
                "RETURNING id"
            ).fetchone()
            assert row is not None
            schedule_id: int = row[0]
        pool = base_db.pool(min_size=1, max_size=1, gate=database_gate)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _poison_backend(pooled)
            with pool.connection() as conn:
                receipt = insert_chat_inbound_once(
                    conn,
                    agent_id=agent_id,
                    content="poison-probe",
                    source="user",
                    payload=None,
                    client_message_id=None,
                    publish_wake=publish_wake,
                )
                assert receipt.inserted
            row, _enabled_changed = update_blocking(pool, schedule_id, {"enabled": False})
            assert row[4] is False
        finally:
            pool.close()


def test_pooled_dial_names_its_process_and_code_version_and_keeps_the_ceiling(
    postgres_access: None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
    throwaway_config: ThrowawayConfig,
) -> None:
    from base import config
    from base.telemetry import process_name

    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        owner = ProcessDbGate(version=lambda: 4321, process=process_name())
        owner.observe_minimum(0)
        assert owner.min_read_due() is False

        monotonic = time.monotonic
        try:
            with monkeypatch.context() as clock:
                clock.setattr(time, "monotonic", lambda: monotonic() + MIN_REFRESH_INTERVAL_S)
                assert owner.min_read_due() is True
                with Database.from_settings(gate=owner).connect() as conn:
                    assert owner.min_read_due() is False
                    assert _statement_timeout(conn) == "1min"
                    with psycopg.connect(admin_console_url(pooled), autocommit=True) as console:
                        cursor = console.execute("SHOW CLIENTS")
                        columns = [column.name for column in cursor.description or ()]
                        names = {
                            row[columns.index("application_name")] for row in cursor.fetchall()
                        }
        finally:
            owner.observe_minimum(0)
    assert f"meiva:{process_name()}:v4321" in names
