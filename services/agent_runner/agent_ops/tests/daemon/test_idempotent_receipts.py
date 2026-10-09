"""Idempotent dispatch stores, replays and protects its owning receipt."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import WakeTriggerKind
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import (
    _fake_spawn_factory,
)
from services.agent_runner.agent_ops.tests.test_daemon import (
    ops_pool as ops_pool,
)


@pytest.mark.asyncio
async def test_idempotent_dispatch_first_run_executes_and_stores(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """The first dispatch with a key executes the op and stores its outcome in
    the shared api_idempotency table (method='ops' rows: path=kind,
    op_status + result)."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    status, result = await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        "key-1",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )

    assert status == "completed"
    assert result == {"id": 777}
    assert calls["n"] == 1
    with ops_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT path, op_status, response_body FROM api_idempotency "
            "WHERE key = %s AND method = 'ops'",
            ("key-1",),
        )
        row = cur.fetchone()
    assert row == ("spawn-launch-v2", "completed", {"id": 777})


@pytest.mark.asyncio
async def test_idempotent_dispatch_replays_without_reexecuting(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A second dispatch with the same key replays the stored outcome — the op
    is NOT re-executed. This is what makes the gateway's retry of a non-
    idempotent op (spawn) safe: a lost response cannot create a twin agent."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    first = await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        "key-2",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    second = await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        "key-2",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )

    assert first == ("completed", {"id": 777})
    assert second == ("completed", {"id": 777})
    assert calls["n"] == 1  # executed exactly once across both dispatches


@pytest.mark.asyncio
async def test_idempotent_dispatch_same_key_waits_for_slow_running_owner(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A duplicate lifecycle request waits within its bounded budget and replays its owner."""
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_STEP_S", 0.01)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_ATTEMPTS", 60)
    calls: dict[str, int] = {}
    started = asyncio.Event()

    async def _slow_dispatch(
        kind: str,
        payload: dict[str, object],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        calls["n"] = calls.get("n", 0) + 1
        started.set()
        await asyncio.sleep(0.3)
        return "completed", {"action": "resume", "agent_id": 777}

    monkeypatch.setattr(daemon, "_dispatch", _slow_dispatch)
    owner = asyncio.create_task(
        daemon._dispatch_idempotent(
            "lifecycle",
            {},
            "slow-lifecycle",
            ops_pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        )
    )
    await started.wait()
    duplicate = await daemon._dispatch_idempotent(
        "lifecycle",
        {},
        "slow-lifecycle",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    first = await owner

    assert duplicate == first == ("completed", {"action": "resume", "agent_id": 777})
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_waiter_fails_after_bounded_wait(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A duplicate wait expires without executing again or claiming completion."""
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_STEP_S", 0.01)
    monkeypatch.setattr(daemon, "_DEDUP_WAIT_ATTEMPTS", 2)
    calls: dict[str, int] = {}
    started = asyncio.Event()

    async def _slow_dispatch(
        kind: str,
        payload: dict[str, object],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
    ) -> tuple[str, dict[str, object]]:
        calls["n"] = calls.get("n", 0) + 1
        started.set()
        await asyncio.sleep(0.3)
        return "completed", {"action": "resume", "agent_id": 777}

    monkeypatch.setattr(daemon, "_dispatch", _slow_dispatch)
    owner = asyncio.create_task(
        daemon._dispatch_idempotent(
            "lifecycle",
            {},
            "stuck-lifecycle",
            ops_pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        )
    )
    await started.wait()
    status, result = await daemon._dispatch_idempotent(
        "lifecycle",
        {},
        "stuck-lifecycle",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    await owner

    assert status == "failed"
    error = str(result["error"])
    assert "never completed" in error
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_distinct_keys_execute_twice(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Different keys are different logical ops — each executes."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        "key-a",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    await daemon._dispatch_idempotent(
        "spawn-launch-v2",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        "key-b",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_idempotent_dispatch_failed_outcome_is_stored_and_replayed(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A business-failed outcome is stored like a success and replayed on a
    same-key retry — a deterministic business failure must not re-run the op."""

    async def _fake_lifecycle(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        catalog: ModelCatalog,
    ):
        raise ValueError("unparseable lifecycle path")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _fake_lifecycle)

    first = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    second = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )

    assert first[0] == "failed"
    assert second == first  # replayed, not re-executed
