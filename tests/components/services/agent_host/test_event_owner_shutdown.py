"""Actual event-writer failure cannot skip host release or replace resource primary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from base.agents.context.clients import ClientSet
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_policy


def event() -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source="system",
        target_agent_id=None,
    )


@pytest.mark.parametrize("native_failure", [False, True])
async def test_host_release_runs_and_first_original_failure_remains_primary(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog, native_failure: bool
) -> None:
    writer_error = TypeError("original event writer failed")
    resource_error = ValueError("original joined resource failed")

    def writer(batch: list[Event]) -> None:
        raise writer_error

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)
    host = AgentHost(
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine="test-box",
        catalog=model_catalog,
        clients=clients,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    release = AsyncMock()
    monkeypatch.setattr("services.agent_runner.agent_host.host.release_hosted_owner", release)
    try:
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError) as writer_observed:
            clients.sync_events(timeout=1)
        assert writer_observed.value is writer_error
        if native_failure:
            scope = await host._resource_service.turn()
            host._resource_service.record_failure(scope, resource_error, name="native")
        with pytest.raises((TypeError, ValueError)) as observed:
            await host.aclose()
        assert observed.value is (resource_error if native_failure else writer_error)
        assert host.resources_joined
        release.assert_awaited_once()
        assert clients.event_pipeline() is pipe
        if native_failure:
            assert any(str(writer_error) in note for note in resource_error.__notes__)
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)


async def test_boot_before_host_closes_writer_even_if_it_failed_and_still_closes_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = TypeError("boot writer failed")

    def writer(batch: list[Event]) -> None:
        raise original

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)
    events: list[str] = []

    async def close_pools(*_args: object) -> None:
        events.append("pools")

    def remove_pidfile(_path: object) -> None:
        events.append("pidfile")

    monkeypatch.setattr(daemon, "_close_host_pools", close_pools)
    monkeypatch.setattr(daemon, "remove_pidfile", remove_pidfile)
    try:
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError):
            clients.sync_events(timeout=1)
        with pytest.raises(TypeError) as observed:
            await daemon._close_process_owners(
                None,
                cast(Any, object()),
                cast(Any, object()),
                None,
                None,
                None,
                None,
                clients=clients,
            )
        assert observed.value is original
        assert events == ["pools", "pidfile"]
        assert clients.event_pipeline() is pipe
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)


async def test_boot_primary_survives_a_failed_constructed_writer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    from ava.sdk_surface.install import Installation
    from base.cluster.machine import MachineIdentity
    from base.config import ConfigBoot

    boot_error = ValueError("original plugin bootstrap defect")
    writer_error = TypeError("boot event writer defect")
    events: list[str] = []

    def writer(batch: list[Event]) -> None:
        raise writer_error

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)

    def boot_handles(
        _database: Database, _config: ConfigBoot
    ) -> tuple[Any, Any, EventBus, Database]:
        return object(), object(), EventBus.from_settings(), database

    def process_clients(**_kwargs: Any) -> ClientSet:
        return clients

    def load_plugins(_config: ConfigBoot, **_kwargs: Any) -> Installation:
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError):
            clients.sync_events(timeout=1)
        raise boot_error

    async def open_pools(*_args: object) -> None:
        pass

    async def close_pools(*_args: object) -> None:
        events.append("pools")

    def no_op(*_args: object) -> None:
        pass

    def acquire(*_args: object) -> bool:
        return True

    def remove_pidfile(_path: object) -> None:
        events.append("pidfile")

    monkeypatch.setattr(daemon, "assert_clock_lattice", no_op)
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "acquire_pidfile", acquire)
    monkeypatch.setattr(daemon, "_boot_handles", boot_handles)
    monkeypatch.setattr(daemon, "_open_host_pools", open_pools)
    monkeypatch.setattr(daemon, "_close_host_pools", close_pools)
    monkeypatch.setattr(daemon, "load_installation", load_plugins)
    monkeypatch.setattr(daemon, "process_clients", process_clients)
    monkeypatch.setattr(daemon, "remove_pidfile", remove_pidfile)
    monkeypatch.setattr("agent.process_boot.init_process_scope", no_op)
    monkeypatch.setattr("agent.process_boot.land_cluster_extensions", no_op)
    machine = MachineIdentity(
        name=lambda: "test-box",
        role=lambda: frozenset({"agent-runner"}),
        host=lambda: "localhost",
        description=lambda: "test host",
    )
    try:
        with pytest.raises(ValueError) as observed:
            await daemon.run(config=ConfigBoot(), database=lambda: database, machine=machine)
        assert observed.value is boot_error
        assert any(str(writer_error) in note for note in boot_error.__notes__)
        assert events == ["pools", "pidfile"]
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)
