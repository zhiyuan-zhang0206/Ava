"""Request dispatch requires its invocation's resource bindings."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import daemon


def test_dispatch_requires_daemon_pool() -> None:
    """An unbound daemon dispatch cannot silently borrow ambient startup state."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch).bind("status_probe", {}, active_ops={}, workers=set())


def test_dispatch_idempotent_requires_daemon_pool() -> None:
    """Idempotency cannot run without its invocation's pool binding."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch_idempotent).bind(
            "spawn-launch-v2", {"agent_id": 1}, "key-4", active_ops={}, workers=set()
        )


@pytest.mark.asyncio
async def test_dispatch_status_probe_passes_the_daemon_pool(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    """The steady-state probe reuses the daemon's already-open central DB pool."""
    from ops.cluster_status import ClusterStatus

    pool: ConnectionPool = ConnectionPool(open=False)
    seen: list[object] = []
    dispatch_pool: ConnectionPool = pool

    def _status(_db: Database, probe_pool: object, *, image: LoadedCommit) -> ClusterStatus:
        assert image is ops_image
        assert _db is ops_database()
        seen.append(probe_pool)
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
        )

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _status)

    status, result = await daemon._dispatch(
        "status_probe",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )

    assert status == "completed"
    assert result["machine_name"] == "win"
    assert seen == [pool]


def test_dispatch_requires_invocation_executor() -> None:
    with pytest.raises(TypeError, match="executor"):
        inspect.signature(daemon._dispatch).bind(
            "status_probe", {}, active_ops={}, workers=set(), pool=ConnectionPool(open=False)
        )


@pytest.mark.parametrize("missing", ["database", "image"])
def test_dispatch_requires_its_database_factory_and_image(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    missing: str,
) -> None:
    resources: dict[str, object] = {
        "active_ops": {},
        "workers": set(),
        "pool": ConnectionPool(open=False),
        "executor": op_executor,
        "catalog": model_catalog,
        "authority": config_authority,
        "database": ops_database,
        "image": ops_image,
    }
    resources.pop(missing)
    with pytest.raises(TypeError, match=missing):
        inspect.signature(daemon._dispatch).bind("status_probe", {}, **resources)
