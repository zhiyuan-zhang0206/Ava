"""Sync/async admission interoperability against the same real PostgreSQL rows."""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import ValidationError

from shared.managed_writer_barrier import ManagedWriterBarrierError
from shared.managed_writer_publication import (
    CurrentAdmission,
    DeferredAdmission,
    LegacyProtocolZero,
    begin_pending_publication,
    publication_admission,
    publication_admission_async,
)
from tests.shared.test_managed_writer_publication import pending, seed_current, unit


@pytest.mark.parametrize(
    "mode",
    [
        "legacy",
        "legacy_lapsed",
        "pending",
        "valid_pending",
        "current",
        "updating_live",
        "updating_lapsed",
        "updating_lapsed_pending",
        "corrupt",
        "empty",
        "missing",
    ],
)
async def test_sync_async_decisions_and_lock_interoperate(  # noqa: PLR0915 — one isolated schema and lock lifetime.
    db_conn: psycopg.Connection, mode: str
) -> None:
    schema = "admission_" + uuid4().hex
    db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for table in ("deployment_state", "machines", "machine_units"):
        db_conn.execute(
            sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
    db_conn.execute("INSERT INTO deployment_state(id) VALUES(1)")
    db_conn.execute("INSERT INTO machines(name) VALUES('runner')")
    db_conn.execute("INSERT INTO machine_units(machine_name,home) VALUES('runner','/ava')")
    current = seed_current(db_conn)
    if mode == "legacy":
        db_conn.execute("UPDATE deployment_state SET managed_writer_evidence=NULL")
    elif mode in {"legacy_lapsed", "updating_live", "updating_lapsed", "updating_lapsed_pending"}:
        # Lease liveness decides whether a non-stable phase still defers: a
        # lapsed lease means the orchestration that set the phase is gone.
        if mode == "legacy_lapsed":
            db_conn.execute("UPDATE deployment_state SET managed_writer_evidence=NULL")
        if mode == "updating_lapsed_pending":
            # A durable pending publication survives lease expiry on purpose;
            # only checked completion/recovery clears it.
            begin_pending_publication(db_conn, pending(db_conn, current))
        if mode == "updating_live":
            db_conn.execute(
                "UPDATE deployment_state SET phase='updating', holder='wsl:pid7', "
                "expires_at=now() + interval '1 hour'"
            )
        else:
            db_conn.execute(
                "UPDATE deployment_state SET phase='updating', holder='wsl:pid7', "
                "expires_at=now() - interval '1 hour'"
            )
    elif mode in {"pending", "valid_pending"}:
        # A live operation before evidence adoption also freezes new births.
        proposal = pending(db_conn, current)
        if mode == "valid_pending":
            begin_pending_publication(db_conn, proposal)
    elif mode in {"corrupt", "empty"}:
        value = '{"version":99}' if mode == "corrupt" else '{"version":2}'
        db_conn.execute("UPDATE deployment_state SET managed_writer_evidence=%s::jsonb", (value,))
    elif mode == "missing":
        db_conn.execute("DELETE FROM deployment_state")
    db_conn.commit()
    args = {"selector_artifact_digest": "b" * 64, "selector_manifest_digest": "c" * 64}
    try:
        async with await psycopg.AsyncConnection.connect(db_conn.info.dsn) as other:
            await other.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
            await other.commit()
            db_conn.execute("BEGIN")
            if mode in {"corrupt", "empty", "missing"}:
                with pytest.raises((ValidationError, ManagedWriterBarrierError)):
                    publication_admission(db_conn, unit(), **args)
                db_conn.rollback()
                async with other.transaction():
                    with pytest.raises((ValidationError, ManagedWriterBarrierError)):
                        await publication_admission_async(other, unit(), **args)
                return
            expected = publication_admission(db_conn, unit(), **args)
            assert isinstance(
                expected,
                {
                    "legacy": LegacyProtocolZero,
                    "legacy_lapsed": LegacyProtocolZero,
                    "pending": DeferredAdmission,
                    "valid_pending": DeferredAdmission,
                    "updating_live": DeferredAdmission,
                    "updating_lapsed": CurrentAdmission,
                    "updating_lapsed_pending": DeferredAdmission,
                    "current": CurrentAdmission,
                }[mode],
            )
            async with other.transaction():
                task = asyncio.create_task(publication_admission_async(other, unit(), **args))
                try:
                    async with asyncio.timeout(5):
                        while db_conn.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                            (other.info.backend_pid,),
                        ).fetchone() != (True,):
                            await asyncio.sleep(0.01)
                    assert not task.done()
                    db_conn.commit()
                    assert await asyncio.wait_for(task, 5) == expected
                finally:
                    db_conn.rollback()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    finally:
        db_conn.rollback()
        db_conn.execute("SELECT set_config('search_path','public',false)")
        db_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db_conn.commit()
