"""Daemon cases: an unrelated op still dispatches while a."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import _stub_pool
from services.agent_runner.agent_ops.tests.test_daemon import (
    ops_pool as ops_pool,
)


@pytest.mark.asyncio
async def test_an_unrelated_op_still_dispatches_while_a_read_is_stuck(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A readiness probe stays reachable while an unrelated worker is blocked."""
    dispatch_pool: ConnectionPool = _stub_pool()
    started = threading.Event()
    release = threading.Event()

    def _wedged() -> object:
        started.set()
        release.wait(timeout=30)
        from ops.rpc_schemas import InventoryReadResult

        return InventoryReadResult(machine="runner", plugins={}, mcp_servers={})

    monkeypatch.setattr(daemon.inventory, "inventory_read_op", _wedged)

    class _Status:
        def model_dump(self: object, *, mode: str) -> dict[str, bool]:
            del mode
            return {"ready": True}

    def _status(_db: Database, _pool: object) -> _Status:
        return _Status()

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _status)

    stuck = asyncio.ensure_future(
        daemon._dispatch(
            "inventory_read",
            {},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        )
    )
    await asyncio.to_thread(started.wait, 10)

    status, result = await daemon._dispatch(
        "status_probe",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert (status, result) == ("completed", {"ready": True})

    release.set()
    await stuck


@pytest.mark.asyncio
async def test_two_config_writes_cannot_interleave(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """`config_write` is a read-modify-write: `write_fields` walks the requested
    keys through dotenv's `set_key`, rewriting the whole `.env` once per key. The
    event loop used to serialize that for free; in a worker thread with
    `ops_concurrency`=8 two of them can interleave, and the later writer lands
    carrying the earlier one's snapshot — the earlier one's fields gone, silently,
    in the only on-disk copy of a cluster's secrets.

    Asserted as non-overlap rather than on a file, so it pins the guarantee (these
    two never run at once) instead of one writer's implementation."""
    dispatch_pool: ConnectionPool = _stub_pool()
    inside = 0
    overlapped = False

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _slow_write(*_a: object, **_kw: object) -> _Result:
        nonlocal inside, overlapped
        inside += 1
        if inside > 1:
            overlapped = True
        time.sleep(0.05)
        inside -= 1
        return _Result()

    monkeypatch.setattr(daemon.host_config, "config_write_op", _slow_write)
    monkeypatch.setattr(daemon.inventory, "inventory_write_op", _slow_write)

    await asyncio.gather(
        daemon._dispatch(
            "config_write",
            {"overrides": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        ),
        daemon._dispatch(
            "config_write",
            {"overrides": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        ),
        daemon._dispatch(
            "inventory_write",
            {"plugins": {}, "mcp_servers": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
        ),
    )

    assert not overlapped, "two state writes ran concurrently — .env can lose fields"


@pytest.mark.asyncio
async def test_config_write_op_receives_actor_and_trace(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """The gateway-stamped actor/trace ride the payload into config_write_op."""
    captured: dict[str, object] = {}

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _capture(*_a: object, **_kw: object) -> _Result:
        captured.update(_kw)
        return _Result()

    dispatch_pool: ConnectionPool = _stub_pool()
    monkeypatch.setattr(daemon.host_config, "config_write_op", _capture)
    status, _ = await daemon._dispatch(
        "config_write",
        {"overrides": {}, "actor": "user_session:administrator", "trace_id": "trace-9"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert captured == {
        "authority": config_authority,
        "local": False,
        "actor": "user_session:administrator",
        "trace_id": "trace-9",
    }


async def test_config_audit_read_op_receives_last(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """The config_audit_read arm forwards `last` into config_audit_read_op."""
    captured: dict[str, object] = {}

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _capture(last: int) -> _Result:
        captured["last"] = last
        return _Result()

    dispatch_pool: ConnectionPool = _stub_pool()
    monkeypatch.setattr(daemon.host_config, "config_audit_read_op", _capture)
    status, _ = await daemon._dispatch(
        "config_audit_read",
        {"last": 7},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "completed"
    assert captured == {"last": 7}


async def test_config_audit_read_rejects_out_of_range_last(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """`last` outside 1..200 fails payload validation before any read."""
    dispatch_pool: ConnectionPool = _stub_pool()
    status, result = await daemon._dispatch(
        "config_audit_read",
        {"last": 201},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert status == "failed"
    assert "last" in str(result["error"])


@pytest.mark.asyncio
async def test_op_arms_do_not_run_on_the_default_executor(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """`asyncio.run` closes by JOINING every default-executor thread, so an arm
    wedged there holds the interpreter's exit after everything else has cleaned up —
    bounded at `THREAD_JOIN_TIMEOUT` (300 s) on 3.12, which is still twenty times the
    supervisor's graceful window. Owning the pool is what decouples the two.

    Asserted on the thread's name rather than on a shutdown timing, because the
    name is the property that both keeps the arm off the default executor AND makes
    the stuck thread findable in a dump — which is what the refusal runbook says to
    go looking for."""
    dispatch_pool: ConnectionPool = _stub_pool()
    seen: list[str] = []

    def _note_thread(*, authority: ConfigAuthority) -> object:
        seen.append(threading.current_thread().name)

        class _R:
            def model_dump(self: object, **_kw: object) -> dict[str, object]:
                return {}

        return _R()

    monkeypatch.setattr(daemon.host_config, "config_read_op", _note_thread)

    await daemon._dispatch(
        "config_read",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )

    assert seen and seen[0].startswith("ava-ops-arm"), f"arm ran on {seen!r}"


def test_shutting_the_pool_down_does_not_wait_for_it(
    monkeypatch: pytest.MonkeyPatch, op_executor: ThreadPoolExecutor
) -> None:
    """The invocation closes the executor without waiting for running arms."""
    calls: list[bool] = []

    def shutdown(wait: bool = True) -> None:
        calls.append(wait)

    monkeypatch.setattr(op_executor, "shutdown", shutdown)
    daemon._shutdown_op_pool(op_executor)
    assert calls == [False]
