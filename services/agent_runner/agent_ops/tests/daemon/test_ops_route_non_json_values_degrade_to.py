"""Daemon cases: ops route non json values degrade to."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.cluster.machines as cluster_machines
from base.cluster.machine import MachineIdentity, MachineRoles
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import boot, daemon
from services.agent_runner.agent_ops.tests.test_daemon import (
    fake_spawn_factory,
    stub_pool,
)
from services.agent_runner.agent_ops.tests.test_daemon import (
    ops_pool as ops_pool,
)
from tests.fixtures.configuration import snapshot_process_config
from tests.fixtures.unit.identity import machine_identity as machine_identity

_CHILD = Path(__file__).resolve().parents[1] / "child_daemon_exit.py"


@pytest.mark.asyncio
async def test_ops_route_non_json_values_degrade_to_str(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A non-JSON-native value in a dispatch result degrades to its str() instead
    of raising in ops_route's json.dumps and 500-ing the /ops control plane.

    The Pydantic arms dump with mode="json"; default=str on the final dumps is
    the last-resort fallback for plain-dict results and any future op.
    """
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    from datetime import UTC

    dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        return "completed", {"at": datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)
    status, body, ctype = await daemon.ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["at"] == "2026-06-11 08:30:00+00:00"


@pytest.mark.asyncio
async def test_ops_route_failed_status_is_still_http_200(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A 'failed' dispatch is a semantic outcome the gateway re-raises, not an
    HTTP error — the envelope carries it at HTTP 200."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        return "failed", {"error": "boom"}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)
    status, body, _ = await daemon.ops_route(
        json.dumps(
            {
                "kind": "spawn-launch",
                "payload": {
                    "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                    "agent_id": 1,
                },
            }
        ).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 200
    assert json.loads(body) == {"status": "failed", "result": {"error": "boom"}}


@pytest.mark.asyncio
async def test_ops_route_malformed_body_400(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Non-JSON body and a body missing `kind` both return 400 without dispatching."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)
    status, body, _ = await daemon.ops_route(
        b"not json",
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 400
    assert "invalid JSON" in json.loads(body)["error"]
    status, body, _ = await daemon.ops_route(
        json.dumps({"payload": {}}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 400
    assert "kind" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_ops_route_crash_becomes_failed_result(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A crash inside _dispatch is caught and returned as a failed result (HTTP 200),
    never leaks as a 500 the gateway can't interpret."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)

    async def _boom(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "_dispatch", _boom)
    status, body, _ = await daemon.ops_route(
        json.dumps(
            {
                "kind": "spawn-launch",
                "payload": {
                    "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                    "agent_id": 1,
                },
            }
        ).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 200
    parsed = json.loads(body)
    assert parsed["status"] == "failed"
    assert "kaboom" in parsed["result"]["error"]
    await asyncio.wait_for(dispatch_sem.acquire(), 1)
    dispatch_sem.release()


def test_ops_route_requires_its_daemon_semaphore() -> None:
    """A route cannot be bound without the daemon's shared concurrency owner."""
    with pytest.raises(TypeError, match="dispatch_sem"):
        inspect.signature(daemon.ops_route).bind(
            b"{}", active_ops={}, workers=set(), requests=set()
        )


@pytest.mark.asyncio
async def test_ops_route_semaphore_caps_concurrency(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Concurrent /ops requests run at most ops_concurrency dispatches in parallel."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    cap = 3
    dispatch_sem = asyncio.Semaphore(cap)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1
        return "completed", {}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)
    bodies = [
        json.dumps(
            {
                "kind": "spawn-launch",
                "payload": {
                    "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
                    "agent_id": i,
                },
            }
        ).encode()
        for i in range(20)
    ]
    results = await asyncio.gather(
        *[
            daemon.ops_route(
                b,
                active_ops={},
                dispatch_sem=dispatch_sem,
                workers=set(),
                requests=set(),
                pool=dispatch_pool,
                executor=op_executor,
                catalog=model_catalog,
                authority=config_authority,
                database=ops_database,
                image=ops_image,
                local_machine=machine_identity,
            )
            for b in bodies
        ]
    )
    assert peak == cap, f"peak concurrency {peak} did not use the shared cap {cap}"
    for _ in range(cap):
        await asyncio.wait_for(dispatch_sem.acquire(), 1)
    assert dispatch_sem.locked()
    for _ in range(cap):
        dispatch_sem.release()
    assert all(status == 200 for status, _, _ in results)


def test_main_logs_and_exits_nonzero_on_an_uncaught_crash(tmp_path: Path) -> None:
    """A crash escaping `_main` still reaches the log with its traceback and still
    leaves a non-zero code for the supervisor.

    Driven in a subprocess because `main` ends in shared `hard_exit` and never
    returns — the price of skipping the interpreter teardown that a wedged arm
    hangs in (see `base.daemon.shutdown`). The contract it used to keep by re-raising is the
    same one asserted here, just observed from outside: logged, and rc != 0."""
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), "crash"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode != 0
    combined = done.stdout + done.stderr
    assert "crashed" in combined and "db pool exploded" in combined


def test_ops_binds_all_interfaces_only_for_a_declared_reachable_address(
    *, machine_identity: MachineIdentity
) -> None:
    """/ops always authenticates; only a host that declares a reachable address
    (`AVA_MACHINE_HOST`) binds 0.0.0.0, a single box binds loopback. Which tokens /ops
    accepts is the write-generation matrix in
    tests/components/lifecycle/db_authority/test_api_tokens.py."""
    machine_identity.set(host="10.0.0.5")
    assert boot.ops_bind_host(machine=machine_identity) == "0.0.0.0"  # noqa: S104
    machine_identity.set(host="localhost")
    assert boot.ops_bind_host(machine=machine_identity) == "127.0.0.1"


def test_register_boot_announces_this_unit_up(
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
    *,
    ops_database: Callable[[], Database],
) -> None:
    """The daemon registers its OWN unit once it is serving — the same
    `register_self` write `ava start` makes, so the stop latch is cleared and
    `up_since_at` restamped by the process whose liveness the row stands for.

    The URL comes from the shared `unit_dial_url()` with this unit's capability
    set, so the daemon cannot advertise a different address than `ava start` did.
    """
    calls: list[str | None] = []
    home = Path("~/.ava")
    machine_identity.set(name="runner-a", role="agent-runner", host="10.0.0.2", description=None)

    def register(
        _db: object,
        *,
        url: str | None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert name == "runner-a"
        assert roles == frozenset({"agent-runner"})
        assert description is None
        assert home == "~/.ava"
        calls.append(url)

    monkeypatch.setattr(cluster_machines, "register_self", register)
    boot.register_boot(
        database=ops_database,
        config=snapshot_process_config(),
        machine=machine_identity,
        ops_port=8600,
        home=home,
    )
    assert calls == ["http://10.0.0.2:8600"]


def test_register_boot_failure_does_not_stop_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
    caplog: pytest.LogCaptureFixture,
    *,
    ops_database: Callable[[], Database],
) -> None:
    """A failed registration refresh leaves dispatch entirely correct, so it is
    logged and swallowed. Exiting here would hand the watchdog a respawn loop and
    take the host dark for the gateway — the outage this call exists to prevent.
    """

    def unreachable(
        _db: object,
        *,
        url: str | None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert url == "http://10.0.0.2:8600"
        assert name == "runner-a"
        assert roles == frozenset({"agent-runner"})
        raise RuntimeError("central postgres unreachable")

    monkeypatch.setattr(cluster_machines, "register_self", unreachable)
    machine_identity.set(name="runner-a", role="agent-runner", host="10.0.0.2")
    boot.register_boot(
        database=ops_database,
        config=snapshot_process_config(),
        machine=machine_identity,
        ops_port=8600,
        home=Path("~/.ava"),
    )
    assert "boot registration failed" in caplog.text
    assert "central postgres unreachable" in caplog.text


def test_register_boot_unstops_a_host_that_came_back(
    db_conn: None, machine_identity: MachineIdentity, *, ops_database: Callable[[], Database]
) -> None:
    """The bug this call fixes, end to end against the real tables.

    A host that announced `ava stop` carries a `stopped_at` latch that only a
    `register_self` clears. When it comes back some other way — an OS-scheduled
    autostart, a watchdog respawn, a rollout's restart leg — nothing used to clear
    it, so the roster reported the host stopped while its ops daemon served every
    op, and the update fan-out (which filters on that same latch) dropped it.
    Registering at the daemon's own boot reconciles both.
    """
    from base.cluster import machines
    from base.config import settings

    config = snapshot_process_config()
    machine_identity.set(name="came-back", role="agent-runner", host="10.0.0.9")
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()

    machines.register_self(
        ops_database(),
        url="http://10.0.0.9:8600",
        name=machine_identity.name(),
        roles=machine_identity.role(),
        description=machine_identity.description(),
        home="~/.ava",
        gateway=config.view.gateway.gateway_url.strip() or None,
    )
    machines.mark_stopping(ops_database(), "came-back", "~/.ava")
    assert machines.list_agent_runners(ops_database()) == []  # dropped from the fan-out
    assert machines.list_stopped_agent_runners(ops_database()) == [
        ("came-back", "http://10.0.0.9:8600")
    ]

    boot.register_boot(
        database=ops_database,
        config=config,
        machine=machine_identity,
        ops_port=8600,
        home=Path("~/.ava"),
    )
    assert machines.list_agent_runners(ops_database()) == [("came-back", "http://10.0.0.9:8600")]
    assert machines.list_stopped_agent_runners(ops_database()) == []


@pytest.mark.asyncio
async def test_ops_route_dedupes_by_envelope_key(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """End-to-end through ops_route: an envelope carrying idempotency_key goes
    through the dedup path — two identical POSTs execute the op once."""
    dispatch_pool: ConnectionPool = ops_pool
    dispatch_sem = asyncio.Semaphore(4)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", fake_spawn_factory(calls))

    body = json.dumps(
        {
            "kind": "spawn-launch",
            "payload": {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 1},
            "idempotency_key": "route-key",
        }
    ).encode()

    code1, payload1, _ = await daemon.ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    code2, payload2, _ = await daemon.ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )

    assert code1 == 200 and code2 == 200
    assert json.loads(payload1) == {"status": "completed", "result": {"id": 777}}
    assert json.loads(payload2) == {"status": "completed", "result": {"id": 777}}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ops_route_without_key_does_not_dedupe(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    ops_pool: ConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """An envelope WITHOUT idempotency_key takes the plain _dispatch path — no
    dedup row is written (idempotent ops have nothing to dedupe)."""
    dispatch_pool: ConnectionPool = ops_pool
    dispatch_sem = asyncio.Semaphore(4)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", fake_spawn_factory(calls))

    body = json.dumps(
        {
            "kind": "spawn-launch",
            "payload": {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 1},
        }
    ).encode()
    await daemon.ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    await daemon.ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )

    assert calls["n"] == 2  # no key → no dedup → both execute
    with ops_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM api_idempotency WHERE method = 'ops'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


@pytest.mark.asyncio
async def test_a_blocking_op_does_not_freeze_the_event_loop(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The 2026-08-12 incident in one assertion. A blocking op on a
    runner stopped returning; because the arm ran inline on the
    loop it took the whole daemon with it — 2 h 03 m with not one line logged, every
    controller stopped, and the stranded-pause self-heal that exists for exactly
    that situation unable to run.

    The op here blocks until the test releases it. What must stay true is that the
    loop keeps turning meanwhile: other coroutines run, other ops dispatch, and the
    health endpoint's `await` still gets its turn."""
    dispatch_pool: ConnectionPool = stub_pool()
    started = threading.Event()
    release = threading.Event()

    def _wedged(
        *_args: object, local_machine: MachineIdentity, **_kw: object
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        started.set()
        release.wait(timeout=30)
        return "completed", {}

    monkeypatch.setattr(daemon, "_dispatch_sync", _wedged)

    task = asyncio.ensure_future(
        daemon._dispatch(
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
        )
    )
    await asyncio.to_thread(started.wait, 10)

    # The loop is still ours: this only completes if nothing is holding it.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 5, "the event loop stopped turning while an op was blocked"

    release.set()
    status, _result = await task
    assert status == "completed"
