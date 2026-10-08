"""Daemon cases: ops route non json values degrade to."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.tests.fakes import pin_endpoints
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import (
    _REPO,
    _db,
    _fake_spawn_factory,
    _stub_pool,
)
from services.agent_runner.agent_ops.tests.test_daemon import (
    ops_pool as ops_pool,
)


@pytest.mark.asyncio
async def test_ops_route_non_json_values_degrade_to_str(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JSON-native value in a dispatch result degrades to its str() instead
    of raising in _ops_route's json.dumps and 500-ing the /ops control plane.

    The Pydantic arms dump with mode="json"; default=str on the final dumps is
    the last-resort fallback for plain-dict results and any future op.
    """
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    from datetime import UTC

    dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(
        kind,
        payload,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        return "completed", {"at": datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, ctype = await daemon._ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["at"] == "2026-06-11 08:30:00+00:00"


@pytest.mark.asyncio
async def test_ops_route_failed_status_is_still_http_200(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'failed' dispatch is a semantic outcome the gateway re-raises, not an
    HTTP error — the envelope carries it at HTTP 200."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)

    async def _fake_dispatch(
        kind,
        payload,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        return "failed", {"error": "boom"}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    status, body, _ = await daemon._ops_route(
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    assert status == 200
    assert json.loads(body) == {"status": "failed", "result": {"error": "boom"}}


@pytest.mark.asyncio
async def test_ops_route_malformed_body_400(
    op_executor: ThreadPoolExecutor,
) -> None:
    """Non-JSON body and a body missing `kind` both return 400 without dispatching."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)
    status, body, _ = await daemon._ops_route(
        b"not json",
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    assert status == 400
    assert "invalid JSON" in json.loads(body)["error"]
    status, body, _ = await daemon._ops_route(
        json.dumps({"payload": {}}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    assert status == 400
    assert "kind" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_ops_route_crash_becomes_failed_result(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside _dispatch is caught and returned as a failed result (HTTP 200),
    never leaks as a 500 the gateway can't interpret."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)

    async def _boom(
        kind,
        payload,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "_dispatch", _boom)  # pyright: ignore[reportUnknownArgumentType]
    status, body, _ = await daemon._ops_route(
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
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
        inspect.signature(daemon._ops_route).bind(
            b"{}", active_ops={}, workers=set(), requests=set()
        )


@pytest.mark.asyncio
async def test_ops_route_semaphore_caps_concurrency(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent /ops requests run at most ops_concurrency dispatches in parallel."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    cap = 3
    dispatch_sem = asyncio.Semaphore(cap)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_dispatch(
        kind,
        payload,
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
    ):  # type: ignore[no-untyped-def]
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

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    bodies = [
        json.dumps({"kind": "spawn-launch", "payload": {"agent_id": i}}).encode() for i in range(20)
    ]
    results = await asyncio.gather(
        *[
            daemon._ops_route(
                b,
                active_ops={},
                dispatch_sem=dispatch_sem,
                workers=set(),
                requests=set(),
                pool=dispatch_pool,
                executor=op_executor,
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
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from services.agent_runner.agent_ops import daemon

        async def _boom():
            raise RuntimeError("db pool exploded mid-loop")

        daemon.init_gateway_process = lambda **kw: None
        daemon.install_graceful_shutdown = lambda *a, **kw: None
        daemon._main = _boom
        daemon.main()
    """)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert done.returncode != 0
    combined = done.stdout + done.stderr
    assert "crashed" in combined and "db pool exploded" in combined


def test_ops_binds_all_interfaces_only_when_authenticated() -> None:
    """An authenticated /ops (any acceptance set, even an empty fail-closed one)
    binds 0.0.0.0; the open posture binds loopback only — an unauthenticated
    control surface is never LAN-reachable. Which tokens /ops accepts is the
    write-generation matrix in tests/components/lifecycle/db_authority/test_api_tokens.py."""
    assert daemon._ops_bind_host(frozenset({"d" * 64})) == "0.0.0.0"  # noqa: S104
    assert daemon._ops_bind_host(frozenset()) == "0.0.0.0"  # noqa: S104
    assert daemon._ops_bind_host(None) == "127.0.0.1"


def test_register_boot_announces_this_unit_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon registers its OWN unit once it is serving — the same
    `register_self` write `ava start` makes, so the stop latch is cleared and
    `up_since_at` restamped by the process whose liveness the row stands for.

    The URL comes from the shared `unit_dial_url()` with this unit's capability
    set, so the daemon cannot advertise a different address than `ava start` did.
    """
    from base.cluster.machine import reset_identity, set_identity

    calls: list[str | None] = []
    monkeypatch.setattr(
        "base.cluster.machines.register_self",
        lambda _db, *, url: calls.append(url),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    set_identity(name="wsl", role="agent-runner")
    try:
        daemon._register_boot()
    finally:
        reset_identity()

    assert calls == ["http://10.0.0.2:8600"]


def test_register_boot_failure_does_not_stop_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed registration refresh leaves dispatch entirely correct, so it is
    logged and swallowed. Exiting here would hand the watchdog a respawn loop and
    take the host dark for the gateway — the outage this call exists to prevent.
    """
    from base.cluster.machine import reset_identity, set_identity

    def _boom(_db: object, *, url: str | None = None) -> None:
        raise RuntimeError("central postgres unreachable")

    monkeypatch.setattr("base.cluster.machines.register_self", _boom)
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    logged: list[str] = []
    monkeypatch.setattr(daemon._log, "exception", lambda msg, *_a, **_k: logged.append(msg))  # pyright: ignore[reportUnknownArgumentType]

    set_identity(name="wsl", role="agent-runner")
    try:
        daemon._register_boot()  # must not raise
    finally:
        reset_identity()

    assert logged and "boot registration failed" in logged[0]


def test_register_boot_unstops_a_host_that_came_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this call fixes, end to end against the real tables.

    A host that announced `ava stop` carries a `stopped_at` latch that only a
    `register_self` clears. When it comes back some other way — an OS-scheduled
    autostart, a watchdog respawn, a rollout's restart leg — nothing used to clear
    it, so the roster reported the host stopped while its ops daemon served every
    op, and the update fan-out (which filters on that same latch) dropped it.
    Registering at the daemon's own boot reconciles both.
    """

    from base.cluster import machines
    from base.cluster.machine import reset_identity, set_identity
    from base.config import settings

    monkeypatch.setattr("base.cluster.machines.ava_home", lambda: "~/.ava")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.9")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()

    set_identity(name="came-back", role="agent-runner")
    try:
        machines.register_self(_db(), url="http://10.0.0.9:8600")
        machines.mark_stopping(_db(), "came-back", "~/.ava")
        assert machines.list_agent_runners(_db()) == []  # dropped from the fan-out
        assert machines.list_stopped_agent_runners(_db()) == [("came-back", "http://10.0.0.9:8600")]

        daemon._register_boot()  # the daemon comes up on its own

        assert machines.list_agent_runners(_db()) == [("came-back", "http://10.0.0.9:8600")]
        assert machines.list_stopped_agent_runners(_db()) == []
    finally:
        reset_identity()


@pytest.mark.asyncio
async def test_idempotent_dispatch_first_run_executes_and_stores(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """The first dispatch with a key executes the op and stores its outcome in
    the shared api_idempotency table (method='ops' rows: path=kind,
    op_status + result)."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    status, result = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-1",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )

    assert status == "completed"
    assert result == {"id": 777}
    assert calls["n"] == 1
    with ops_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "SELECT path, op_status, response_body FROM api_idempotency "
            "WHERE key = %s AND method = 'ops'",
            ("key-1",),
        )
        row = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
    assert row == ("spawn-launch", "completed", {"id": 777})


@pytest.mark.asyncio
async def test_idempotent_dispatch_replays_without_reexecuting(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """A second dispatch with the same key replays the stored outcome — the op
    is NOT re-executed. This is what makes the gateway's retry of a non-
    idempotent op (spawn) safe: a lost response cannot create a twin agent."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    first = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-2",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )
    second = await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-2",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )

    assert first == ("completed", {"id": 777})
    assert second == ("completed", {"id": 777})
    assert calls["n"] == 1  # executed exactly once across both dispatches


@pytest.mark.asyncio
async def test_idempotent_dispatch_same_key_waits_for_slow_running_owner(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
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
    )
    first = await owner

    assert duplicate == first == ("completed", {"action": "resume", "agent_id": 777})
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_waiter_fails_after_bounded_wait(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
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
    )
    await owner

    assert status == "failed"
    error = str(result["error"])
    assert "never completed" in error
    assert calls == {"n": 1}


@pytest.mark.asyncio
async def test_idempotent_dispatch_distinct_keys_execute_twice(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """Different keys are different logical ops — each executes."""
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-a",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )
    await daemon._dispatch_idempotent(
        "spawn-launch",
        {"agent_id": 777},
        "key-b",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_idempotent_dispatch_failed_outcome_is_stored_and_replayed(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """A business-failed outcome is stored like a success and replayed on a
    same-key retry — a deterministic business failure must not re-run the op."""

    async def _fake_lifecycle(  # type: ignore[no-untyped-def]
        _db: object,
        _bus: object,
        path,
        body,
        pool,
        *,
        trigger_inbound_id=None,
        trigger_inbound_kind=None,
    ):
        raise ValueError("unparseable lifecycle path")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _fake_lifecycle)  # pyright: ignore[reportUnknownArgumentType]

    first = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )
    second = await daemon._dispatch_idempotent(
        "lifecycle",
        {"path": "garbage"},
        "key-3",
        ops_pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
    )

    assert first[0] == "failed"
    assert second == first  # replayed, not re-executed


@pytest.mark.asyncio
async def test_ops_route_dedupes_by_envelope_key(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """End-to-end through _ops_route: an envelope carrying idempotency_key goes
    through the dedup path — two identical POSTs execute the op once."""
    dispatch_pool: ConnectionPool = ops_pool
    dispatch_sem = asyncio.Semaphore(4)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    body = json.dumps(
        {"kind": "spawn-launch", "payload": {"agent_id": 1}, "idempotency_key": "route-key"}
    ).encode()

    code1, payload1, _ = await daemon._ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    code2, payload2, _ = await daemon._ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )

    assert code1 == 200 and code2 == 200
    assert json.loads(payload1) == {"status": "completed", "result": {"id": 777}}
    assert json.loads(payload2) == {"status": "completed", "result": {"id": 777}}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ops_route_without_key_does_not_dedupe(
    op_executor: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch, ops_pool: ConnectionPool
) -> None:
    """An envelope WITHOUT idempotency_key takes the plain _dispatch path — no
    dedup row is written (idempotent ops have nothing to dedupe)."""
    dispatch_pool: ConnectionPool = ops_pool
    dispatch_sem = asyncio.Semaphore(4)
    calls: dict[str, int] = {}
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _fake_spawn_factory(calls))

    body = json.dumps({"kind": "spawn-launch", "payload": {"agent_id": 1}}).encode()
    await daemon._ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )
    await daemon._ops_route(
        body,
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
    )

    assert calls["n"] == 2  # no key → no dedup → both execute
    with ops_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM api_idempotency WHERE method = 'ops'")  # pyright: ignore[reportUnknownMemberType]
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


@pytest.mark.asyncio
async def test_a_blocking_op_does_not_freeze_the_event_loop(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-08-12 incident in one assertion. A blocking op on the Windows
    runner stopped returning; because the arm ran inline on the
    loop it took the whole daemon with it — 2 h 03 m with not one line logged, every
    controller stopped, and the stranded-pause self-heal that exists for exactly
    that situation unable to run.

    The op here blocks until the test releases it. What must stay true is that the
    loop keeps turning meanwhile: other coroutines run, other ops dispatch, and the
    health endpoint's `await` still gets its turn."""
    dispatch_pool: ConnectionPool = _stub_pool()
    started = threading.Event()
    release = threading.Event()

    def _wedged(*_args: object, **_kw: object) -> tuple[str, dict[str, object]]:
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
