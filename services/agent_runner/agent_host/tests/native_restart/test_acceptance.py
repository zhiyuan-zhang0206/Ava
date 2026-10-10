"""Guarded restart freezes one command and uses the original lifecycle proof."""

from concurrent.futures import ThreadPoolExecutor
from typing import LiteralString

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.agents.messages.native_restart import (
    NativeRestartConflictError,
    accept_native_restart,
    lookup_native_restart,
    native_restart_progress,
)
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


def _overlay(request: NativeRestartRequest) -> dict[str, object] | None:
    return request.config_overlay


async def test_same_key_concurrent_replay_survives_source_and_owner_cleanup(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    request = NativeRestartRequest(target=target, config_overlay={"max_turns": 9})
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        with ThreadPoolExecutor(2) as executor:
            pending = [
                executor.submit(
                    accept_native_restart,
                    pool,
                    "same",
                    target.agent_id,
                    request,
                    _overlay,
                    catalog=model_catalog,
                    llm_override=config_authority.all_domains.lm.llm_override,
                    default_model=config_authority.all_domains.lm.llm_model,
                )
                for _ in range(2)
            ]
            first, second = [future.result(timeout=10) for future in pending]
        assert first == second
        with pytest.raises(NativeRestartConflictError):
            accept_native_restart(
                pool,
                "other",
                target.agent_id,
                request,
                _overlay,
                catalog=model_catalog,
                llm_override=config_authority.all_domains.lm.llm_override,
                default_model=config_authority.all_domains.lm.llm_model,
            )
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (target.agent_id,)
        )
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (first.command_id,))
        db_conn.execute(
            "UPDATE agents_meta SET config_overlay='{}',native_work_id=NULL WHERE id=%s",
            (target.agent_id,),
        )
        db_conn.commit()

        def forbidden(_request: NativeRestartRequest) -> dict[str, object]:
            raise AssertionError("replay must not consult mutable overlay")

        assert (
            accept_native_restart(
                pool,
                "same",
                target.agent_id,
                request,
                forbidden,
                catalog=model_catalog,
                llm_override=config_authority.all_domains.lm.llm_override,
                default_model=config_authority.all_domains.lm.llm_model,
            )
            == first
        )
        assert lookup_native_restart(pool, "same", target.agent_id, request) == first
        progress = native_restart_progress(pool, target.agent_id, first.command_id)
        assert progress is not None and progress.outcome == "uncertain"
        assert progress.reason == "source_command_unavailable"


@pytest.mark.parametrize("changed", [True, 1.0, 2])
async def test_raw_json_conflict_is_not_python_numeric_equality(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    changed: object,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    original = NativeRestartRequest(target=target, config_overlay={"limit": 1})
    changed_request = NativeRestartRequest(target=target, config_overlay={"limit": changed})
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accept_native_restart(
            pool,
            "raw",
            target.agent_id,
            original,
            _overlay,
            catalog=model_catalog,
            llm_override=config_authority.all_domains.lm.llm_override,
            default_model=config_authority.all_domains.lm.llm_model,
        )
        with pytest.raises(NativeRestartConflictError):
            lookup_native_restart(pool, "raw", target.agent_id, changed_request)
        with pytest.raises(NativeRestartConflictError):
            accept_native_restart(
                pool,
                "raw",
                target.agent_id,
                changed_request,
                _overlay,
                catalog=model_catalog,
                llm_override=config_authority.all_domains.lm.llm_override,
                default_model=config_authority.all_domains.lm.llm_model,
            )


@pytest.mark.parametrize(
    "damage", ["done", "foreign_owner", "observation_without_apply", "fake_superseded"]
)
async def test_unknown_source_transition_is_not_execution_or_no_effect_proof(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    damage: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    request = NativeRestartRequest(target=target)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = accept_native_restart(
            pool,
            "bad-source",
            target.agent_id,
            request,
            _overlay,
            catalog=model_catalog,
            llm_override=config_authority.all_domains.lm.llm_override,
            default_model=config_authority.all_domains.lm.llm_model,
        )
        statements: dict[str, LiteralString] = {
            "done": "status='done'",
            "foreign_owner": "target_owner=gen_random_uuid()",
            "observation_without_apply": "observed_at=now(),status='done'",
            "fake_superseded": 'status=\'done\',payload=\' {"lifecycle_result":{"outcome":"superseded","reason":"force_terminate"}}\'::jsonb',
        }
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (target.agent_id,)
        )
        if damage == "observation_without_apply":
            with pytest.raises(psycopg.errors.CheckViolation):
                db_conn.execute(
                    sql.SQL("UPDATE inbound_messages SET {} WHERE id=%s").format(
                        sql.SQL(statements[damage])
                    ),
                    (accepted.command_id,),
                )
            db_conn.rollback()
            progress = native_restart_progress(pool, target.agent_id, accepted.command_id)
            assert progress is not None and progress.outcome == "accepted"
            assert progress.applied_at is None and progress.observed_at is None
            return
        db_conn.execute(
            sql.SQL("UPDATE inbound_messages SET {} WHERE id=%s").format(
                sql.SQL(statements[damage])
            ),
            (accepted.command_id,),
        )
        db_conn.commit()
        progress = native_restart_progress(pool, target.agent_id, accepted.command_id)
        assert progress is not None and progress.outcome == "uncertain"
        assert progress.applied_at is None and progress.observed_at is None


@pytest.mark.parametrize("fence", ["resurrect", "force_terminate", "target_replaced"])
async def test_actual_original_supersession_owner_retains_no_effect_fact(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    fence: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    from uuid import uuid4

    from agent.ownership.lifecycle_intent import accept_lifecycle_intent, settle_superseded_intent
    from base.agents.incarnation.lifecycle_acceptance import (
        supersede_lifecycle_for_force,
        supersede_lifecycle_for_resurrect,
    )
    from base.db.transaction import async_write_transaction

    incarnation, target = await managed_work(db_conn, aops_pool)
    request = NativeRestartRequest(target=target)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = accept_native_restart(
            pool,
            "original-no-effect",
            target.agent_id,
            request,
            _overlay,
            catalog=model_catalog,
            llm_override=config_authority.all_domains.lm.llm_override,
            default_model=config_authority.all_domains.lm.llm_model,
        )
        if fence == "target_replaced":
            async with async_write_transaction(aops_pool) as conn:
                intent = await accept_lifecycle_intent(
                    conn, target.agent_id, incarnation=incarnation
                )
                assert intent is not None
            db_conn.execute(
                "UPDATE agents_meta SET runtime_generation=%s WHERE id=%s",
                (uuid4(), target.agent_id),
            )
            db_conn.commit()
            async with async_write_transaction(aops_pool) as conn:
                assert await settle_superseded_intent(conn, intent)
        else:
            with db_conn.transaction():
                kind = "resurrect" if fence == "resurrect" else "terminate"
                row = db_conn.execute(
                    "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user') RETURNING id",
                    (target.agent_id, kind),
                ).fetchone()
                assert row is not None
                if fence == "resurrect":
                    supersede_lifecycle_for_resurrect(db_conn, target.agent_id, row[0])
                else:
                    db_conn.execute(
                        "UPDATE agents_meta SET last_force_terminate_inbound_id=%s WHERE id=%s",
                        (row[0], target.agent_id),
                    )
                    supersede_lifecycle_for_force(db_conn, target.agent_id, row[0])
            db_conn.commit()
        progress = native_restart_progress(pool, target.agent_id, accepted.command_id)
        assert progress is not None and progress.outcome == "superseded"
        assert progress.reason == fence
        assert progress.applied_at is None and progress.observed_at is None
        assert (
            lookup_native_restart(pool, "original-no-effect", target.agent_id, request) == accepted
        )
