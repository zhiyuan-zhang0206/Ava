"""The service owns timed-out pass proxies without joining blocked worker threads."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from base.daemon.health import LivenessGroup, LoopProgress
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config


@pytest.mark.parametrize(
    "failure", [RuntimeError("unknown failure"), psycopg.ProgrammingError("drift")]
)
async def test_completed_failure_preserves_the_original_exception(failure: Exception) -> None:
    progress = LoopProgress("dispatch", timeout_s=1.0)

    def fail(_pool: object) -> None:
        raise failure

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(type(failure)) as raised:
            await daemon._maintenance_with_liveness(_FAKE_POOL, progress, fail, tasks=tasks)
        assert raised.value is failure
        assert not progress.snapshot()["wedged"]


# Passes never touch this pool; the service test supplies its own close recorder.
_FAKE_POOL: Any = object()


async def test_late_failure_is_reported_without_reviving_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    progress = LoopProgress("dispatch", timeout_s=0.01)
    release = threading.Event()
    failure = RuntimeError("late worker failure")

    def fail_after_release(_pool: object) -> None:
        assert release.wait(5.0), "test did not release its worker"
        raise failure

    with caplog.at_level(logging.ERROR, logger=daemon._log.name):
        try:
            async with asyncio.TaskGroup() as tasks:
                with pytest.raises(daemon.WedgedPassError):
                    await daemon._maintenance_with_liveness(
                        _FAKE_POOL, progress, fail_after_release, tasks=tasks
                    )
                assert progress.snapshot()["wedged"]
                release.set()
        finally:
            release.set()

    records = [record for record in caplog.records if "after its hard deadline" in record.message]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is failure
    assert records[0].exc_info[2] is not None
    assert progress.snapshot()["wedged"]


def _mock_service(
    monkeypatch: pytest.MonkeyPatch,
    work: Callable[..., None],
    trackers: list[LivenessGroup],
    closed: list[str],
) -> None:
    async def health(
        _name: str,
        _port: int,
        *,
        liveness: LivenessGroup,
        components: Callable[[], list[dict[str, object]]],
    ) -> object:
        trackers.append(liveness)
        return object()

    async def stop_health(_server: object) -> None:
        closed.append("health")

    # Shared by group-injected resolution and the registry gauge's plain call.
    async def sibling(*_args: object, tasks: asyncio.TaskGroup | None = None) -> None:
        await asyncio.Event().wait()

    class Pool:
        def close(self) -> None:
            closed.append("pool")

    def pool(_self: object) -> Pool:
        return Pool()

    config = events_maintenance_config(events_maintenance_pass_deadline_s=0.02)
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: closed.append("pidfile"))
    monkeypatch.setattr(daemon, "start_health_server", health)
    monkeypatch.setattr(daemon, "stop_health_server", stop_health)
    monkeypatch.setattr(daemon, "_run_maintenance", work)
    monkeypatch.setattr(daemon, "_resolution_loop", sibling)
    monkeypatch.setattr(daemon.registry_gauge, "registry_gauge_loop", sibling)
    monkeypatch.setattr(daemon.Database, "pool", pool)
    monkeypatch.setattr(daemon.admission, "quiesced", lambda: False)
    monkeypatch.setattr(daemon, "events_maintenance_config", lambda: config)


async def test_service_stop_cancels_expired_proxy_before_worker_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    trackers: list[LivenessGroup] = []
    closed: list[str] = []

    def blocking_pass(*_args: object) -> None:
        started.set()
        try:
            assert release.wait(5.0), "test did not release its worker"
        finally:
            finished.set()

    _mock_service(monkeypatch, blocking_pass, trackers, closed)

    async def wait_for_wedge() -> None:
        while not trackers or not trackers[0].snapshot()["dispatch"]["wedged"]:
            await asyncio.sleep(0.005)

    before = asyncio.all_tasks()
    async with asyncio.TaskGroup() as tests:
        service = tests.create_task(daemon.run())
        probe = tests.create_task(wait_for_wedge())
        try:
            done, _ = await asyncio.wait({probe}, timeout=1.0)
            assert probe in done, "the pass did not fail health before its worker completed"
            assert started.is_set()
            assert not finished.is_set()
            service.cancel()
            done, _ = await asyncio.wait({service}, timeout=1.0)
            assert service in done, "service shutdown joined the blocked worker"
            assert service.cancelled()
            assert asyncio.all_tasks() - before - {service, probe} == set(), (
                "the stopped service left an unowned pass proxy alive"
            )
            assert not finished.is_set()
            assert sorted(closed) == ["health", "pidfile", "pool"]
            assert trackers[0].snapshot()["dispatch"]["wedged"]
        finally:
            release.set()
            probe.cancel()
            service.cancel()
