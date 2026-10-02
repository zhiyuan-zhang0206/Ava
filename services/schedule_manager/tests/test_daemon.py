"""The service's structure: the TaskGroup that owns the two loops, a crash that ends
the process, the checkout guard, and the gateway no longer owning a manager."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

import base.db
import gateway.app
from base.daemon.loop_health import LivenessGroup
from base.deploy.maintenance import admission
from services.schedule_manager import daemon


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2)
    try:
        yield p
    finally:
        p.close()


def test_the_gateway_owns_no_schedule_manager() -> None:
    """The manager is its own service: the gateway module is gone and its lifespan
    neither builds nor starts one, so a gateway restart cannot touch schedules."""
    assert importlib.util.find_spec("gateway.schedules.manager") is None
    assert not hasattr(gateway.app, "ScheduleManager")


async def test_both_loops_run_their_rounds(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciled = asyncio.Event()
    consumed = asyncio.Event()

    def reconcile(_self: object) -> None:
        reconciled.set()

    def consume(_pool: object, _manager: object) -> int:
        consumed.set()
        return 0

    monkeypatch.setattr(daemon.ScheduleManager, "reconcile", reconcile)
    monkeypatch.setattr(daemon.requests, "consume_requests", consume)
    liveness = LivenessGroup()

    task = asyncio.create_task(daemon._run_loops(pool, liveness))
    try:
        await asyncio.wait_for(asyncio.gather(reconciled.wait(), consumed.wait()), timeout=10)
        assert set(liveness.snapshot()) == {"reconcile", "requests"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("crashing", ["reconcile", "requests"])
async def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch, crashing: str
) -> None:
    """A backend or database failure that is not a lost connection ends the
    process; the supervisor restarts it."""
    cancelled: list[str] = []

    def stand_in(name: str):
        async def loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} loop crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return loop

    async def run_rounds(name: str, *_args: object) -> None:
        await stand_in(name)()

    monkeypatch.setattr(daemon.round_loop, "run_rounds", run_rounds)

    with pytest.raises(ExceptionGroup) as raised:
        await daemon._run_loops(pool, LivenessGroup())

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} loop crashed"]
    assert cancelled == [({"reconcile", "requests"} - {crashing}).pop()]


async def test_a_crash_leaves_run_after_releasing_its_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    class _Pool:
        def close(self) -> None:
            released.append("pool")

    async def start_health(_name: str, _port: int, **_kw: object) -> str:
        return "health"

    async def stop_health(_server: object) -> None:
        released.append("health")

    async def crashing_loops(_pool: object, _liveness: object) -> None:
        raise ExceptionGroup("loops", [RuntimeError("reconcile loop crashed")])

    async def no_provision(_pool: object) -> None:
        return None

    def acquire(_path: object, _module: str) -> bool:
        return True

    def remove(_path: object) -> None:
        released.append("pidfile")

    def make_pool(_self: object, **_kw: object) -> _Pool:
        return _Pool()

    def own_checkout(_repo: object) -> None:
        return None

    monkeypatch.setattr(daemon, "prod_service_checkout_error", own_checkout)
    monkeypatch.setattr(daemon, "acquire_pidfile", acquire)
    monkeypatch.setattr(daemon, "remove_pidfile", remove)
    monkeypatch.setattr(daemon, "start_health_server", start_health)
    monkeypatch.setattr(daemon, "stop_health_server", stop_health)
    monkeypatch.setattr(daemon.Database, "pool", make_pool)
    monkeypatch.setattr(daemon, "provision_builtins", no_provision)
    monkeypatch.setattr(daemon, "_run_loops", crashing_loops)

    with pytest.raises(ExceptionGroup):
        await daemon.run()

    assert sorted(released) == ["health", "pidfile", "pool"]


async def test_a_foreign_checkout_refuses_to_supervise_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #194: a service running from a worktree against the prod home must not
    supervise schedules — it would run un-reviewed code and die with the worktree.
    It fails at boot instead of carrying on."""

    def foreign(_repo: object) -> str:
        return "prod home but foreign checkout"

    monkeypatch.setattr(daemon, "prod_service_checkout_error", foreign)

    with pytest.raises(RuntimeError, match="foreign checkout"):
        await daemon.run()


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_polls_the_request_table_through_no_connection(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    """The request consumer reads the table every second even while holds are up
    (so a request stays queued); inside the stop window it must not borrow."""
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)

    def no_reconcile(_self: object) -> None:
        return None

    monkeypatch.setattr(daemon.ScheduleManager, "reconcile", no_reconcile)
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []

    task = asyncio.create_task(daemon._run_loops(pool, LivenessGroup()))
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.connection.called) is (not quiesced)
