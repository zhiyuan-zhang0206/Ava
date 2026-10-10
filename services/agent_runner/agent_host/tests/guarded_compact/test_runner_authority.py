"""Actual runner login, not an administrator, produces and completes compact proof."""

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from base.cluster.authority import GATEWAY_GROUP, RUNNER_GROUP, Groups, ensure_groups
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


async def test_real_runner_login_closes_compact_without_admission_or_metadata_insert_authority(
    db_conn: psycopg.Connection,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    model_catalog: ModelCatalog,
) -> None:
    role = "compact_runner_" + uuid4().hex
    password = uuid4().hex
    with psycopg.connect(db_conn.info.dsn, autocommit=True) as admin:
        ensure_groups(
            admin,
            owner=db_conn.info.user,
            database=db_conn.info.dbname,
            groups=Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP),
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        admin.execute(sql.SQL("GRANT ava_runner TO {}").format(sql.Identifier(role)))
    dsn = make_conninfo(db_conn.info.dsn, user=role, password=password)
    model = SummaryModel(responses=["Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    try:
        async with AsyncConnectionPool[psycopg.AsyncConnection](
            dsn, min_size=1, max_size=1, open=False
        ) as pool:
            accepted = await admit(db_conn, pool, client, monkeypatch, catalog=model_catalog)
            await accepted.host.run_turn(accepted.agent)
            status = accepted.status(client)
            assert status["outcome"] == "applied" and status["continuation_released"]
            assert model.calls == 1
            async with pool.connection() as conn:
                assert await (await conn.execute("SELECT current_user")).fetchone() == (role,)
                await assert_admission_denied(conn)
    finally:
        with db_conn.transaction():
            db_conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            db_conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def assert_admission_denied(conn: psycopg.AsyncConnection) -> None:
    for statement in (
        "INSERT INTO agents_meta(id) VALUES(1)",
        "INSERT INTO native_compact_commands DEFAULT VALUES",
        "DELETE FROM native_compact_commands",
        "UPDATE native_compact_observations SET resources='{}'",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute(statement)
