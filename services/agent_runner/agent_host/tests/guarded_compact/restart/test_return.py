"""An original compact execution closes its accepted restart before any new work."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.agents.messages.native_cancel import accept_native_cancel, observe_native_work
from base.agents.messages.native_restart import accept_native_restart, native_restart_progress
from base.config import settings
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("lost", ["none", "before_apply", "after_apply", "after_observe_cleanup"])
@pytest.mark.parametrize("mode", ["success", "failure", "cancel_first", "restart_first"])
async def test_original_compact_restart_without_second_ordinary_work(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    mode: str,
    lost: str,
    model_catalog: ModelCatalog,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class WaitingModel(SummaryModel):
        async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("original")
            entered.set()
            await release.wait()
            if mode == "failure":
                raise psycopg.OperationalError("test original provider response unknown")
            return await super().ainvoke(*args, **kwargs)

    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog,
        {
            "gpt-": replace(
                binding,
                build_single_attempt=lambda _: WaitingModel(
                    responses=["Original compact result. " * 100]
                ),
            )
        },
    )
    injected = install_apply_loss(monkeypatch, aops_pool, db_conn, lost)
    accepted = await admit(db_conn, aops_pool, client, monkeypatch, catalog=model_catalog)
    running = asyncio.create_task(accepted.host.run_turn(accepted.agent))
    cancelled = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            target = await asyncio.to_thread(observe_native_work, pool, accepted.agent)
            assert target is not None
            if mode == "cancel_first":
                cancelled = await asyncio.to_thread(
                    accept_native_cancel, pool, "compact-cancel", accepted.agent, target
                )
            restart = await asyncio.to_thread(
                accept_native_restart,
                pool,
                "compact-restart",
                accepted.agent,
                NativeRestartRequest(target=target),
                lambda _: None,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                default_model=settings.lm.llm_model,
            )
            if mode == "restart_first":
                cancelled = await asyncio.to_thread(
                    accept_native_cancel, pool, "compact-cancel", accepted.agent, target
                )
            queued = _insert(db_conn, accepted.agent)
            release.set()
            await asyncio.wait_for(running, 10)
            progress = await asyncio.to_thread(
                native_restart_progress, pool, accepted.agent, restart.command_id
            )
        assert injected == ([] if lost == "none" else [lost])
        assert progress is not None and progress.outcome == (
            "observed" if lost == "after_observe_cleanup" else "applied"
        )
        assert progress.acceptance.target == target and progress.applied_at is not None
        assert len(accepted.ordinary) == 1 and calls == ["original"]
        assert_original_closed(db_conn, queued, target.work_id, cancelled)
        if lost != "after_observe_cleanup":
            await accepted.host.run_turn(accepted.agent)
            assert len(accepted.ordinary) == 2 and calls == ["original"]
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def assert_original_closed(
    db_conn: psycopg.Connection,
    queued: int,
    work_id: Any,
    cancelled: Any,
) -> None:
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
    ).fetchone() == ("pending",)
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (work_id,),
    ).fetchone() == ("settled", True)
    if cancelled is not None:
        assert db_conn.execute(
            "SELECT outcome FROM native_cancel_commands WHERE id=%s", (cancelled.command_id,)
        ).fetchone() == ("applied",)


def install_apply_loss(
    monkeypatch: pytest.MonkeyPatch,
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    lost: str,
) -> list[str]:
    from services.agent_runner.agent_host.invocation.compact import lifecycle

    original_apply = lifecycle.apply_hosted_lifecycle
    injected: list[str] = []

    async def apply(*args: Any, **kwargs: Any) -> Any:
        if lost != "none" and not injected:
            injected.append(lost)
            if lost in ("after_apply", "after_observe_cleanup"):
                await original_apply(*args, **kwargs)
            if lost == "after_observe_cleanup":
                from agent.tests.claim.test_inbound_ownership import _admit

                await _admit(aops_pool, args[1].agent_id)
                db_conn.execute(
                    "DELETE FROM inbound_messages WHERE id=%s", (kwargs["expected_command_id"],)
                )
                db_conn.commit()
            raise psycopg.OperationalError("test lost original compact restart response")
        return await original_apply(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "apply_hosted_lifecycle", apply)
    return injected
