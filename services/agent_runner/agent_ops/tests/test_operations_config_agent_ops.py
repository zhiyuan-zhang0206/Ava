from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops import host_config
from ops.rpc_schemas import ConfigReadResult, ConfigWriteOpResult


@pytest.mark.asyncio
async def test_dispatch_config_read_calls_config_read_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    captured: list[bool] = []

    def _fake_config_read(
        *, authority: ConfigAuthority, local_machine: str, host: Host
    ) -> ConfigReadResult:
        del local_machine
        captured.append(True)
        return ConfigReadResult(machine="x", host_fields={}, raw_overrides={})

    monkeypatch.setattr(host_config, "config_read_op", _fake_config_read)
    status, result = await daemon._dispatch(
        "config_read",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "completed"
    assert result["machine"] == "x"
    assert captured == [True]


@pytest.mark.asyncio
async def test_dispatch_config_write_passes_overrides(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    captured: dict[str, Any] = {}

    def _fake_config_write(
        overrides: dict[str, Any],
        *,
        local: bool = False,
        actor: str | None = None,
        trace_id: str | None = None,
        authority: ConfigAuthority,
        local_machine: str,
        host: Host,
    ) -> ConfigWriteOpResult:
        del local_machine
        captured["overrides"] = overrides
        captured["local"] = local
        captured["actor"] = actor
        captured["trace_id"] = trace_id
        return ConfigWriteOpResult(machine="x", results={}, applied=True, restart_required=[])

    monkeypatch.setattr(host_config, "config_write_op", _fake_config_write)
    status, _result = await daemon._dispatch(
        "config_write",
        {"overrides": {"ops_concurrency": 2}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "completed"
    assert captured["overrides"] == {"ops_concurrency": 2}
    assert captured["actor"] is None
    assert captured["trace_id"] is None


@pytest.mark.asyncio
async def test_dispatch_config_write_missing_overrides_key_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    status, result = await daemon._dispatch(
        "config_write",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "failed"
    assert "overrides" in str(result["error"])
