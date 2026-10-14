"""Actual resource join bounds return without certifying pending clients as closed."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.agent_runner.agent_host.host as agent_host_host
from ava.sdk_surface.installation import Installation
from base.agents.context.clients import ClientSet
from base.agents.sdk import call_policy
from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy

from ... import daemon
from ...host import AgentHost


async def test_sampling_failure_is_collected_before_pools_and_pidfile_cleanup(
    monkeypatch: pytest.MonkeyPatch, model_installation: Installation
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = TypeError("sampling reader defect")

    def read() -> call_policy.SamplingPolicy:
        entered.set()
        assert release.wait(5)
        raise original

    sampling = call_policy.SamplingPolicyOwner(reader=read)
    sampling.read()
    assert entered.wait(2)
    release.set()
    worker = sampling.worker
    assert worker is not None and worker.completed.wait(2)
    events: list[str] = []

    async def close_pools(*_args: object) -> None:
        assert not worker.thread.is_alive()
        events.append("pools")

    monkeypatch.setattr(daemon, "close_host_pools", close_pools)

    def remove_pidfile(_path: object) -> None:
        events.append("pidfile")

    monkeypatch.setattr(daemon, "remove_pidfile", remove_pidfile)
    with pytest.raises(TypeError) as caught:
        await daemon.close_process_owners(
            None,
            cast(Any, object()),
            cast(Any, object()),
            replace(model_installation, sampling=sampling),
            None,
            None,
            None,
        )
    assert caught.value is original and sampling.error is not None
    assert sampling.error[0] is original
    assert events == ["pools", "pidfile"]


async def test_original_error_joins_before_clients_and_pools_close(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog, *, database_gate: ProcessDbGate
) -> None:
    host = AgentHost(
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine=configured_machine(name="test-box"),
        catalog=model_catalog,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        commands=CommandCatalog(skills=list),
    )
    scope = await host._resource_service.turn()
    failure = ValueError("original late resource error")
    host._resource_service.record_failure(scope, failure, name="original")
    events: list[str] = []
    monkeypatch.setattr(host._clients, "close", lambda: events.append("clients"))
    monkeypatch.setattr(agent_host_host, "release_hosted_owner", AsyncMock())

    async def close_pools(*_args: object) -> None:
        events.append("pools")

    monkeypatch.setattr(daemon, "close_host_pools", close_pools)
    with pytest.raises(ValueError) as observed:
        await host.aclose()
    assert observed.value is failure
    assert host.resources_joined
    await daemon.close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))
    assert events == ["clients", "pools"]


def exercise_unfinished_main() -> None:
    """Production main's existing hard exit must skip a still-owned async task."""
    from base.deploy.schema import migrations

    def schema_gate(*_args: object) -> None:
        pass

    migrations.assert_schema_current = schema_gate

    async def run(
        *,
        config: ConfigBoot,
        database: Callable[[], Database],
        machine: MachineIdentity,
        home: Path,
        timers: Timers,
        clients: ClientSet,
        on_clients_owned: Callable[[], None],
    ) -> None:
        assert home == Path(os.environ["AVA_HOME"])
        assert timers is SYSTEM_TIMERS
        on_clients_owned()
        host = AgentHost(
            policy=configured_policy(),
            pool=cast(Any, MagicMock()),
            checkpointer=cast(Any, object()),
            graph=cast(Any, object()),
            machine=configured_machine(name="resource-join-test"),
            catalog=cast(ModelCatalog, MagicMock()),
            bus=EventBus.from_settings(),
            db=database(),
            clients=clients,
            commands=CommandCatalog(skills=list),
        )
        marker = Path(os.environ["RESOURCE_JOIN_MARKER"])
        patch = pytest.MonkeyPatch()
        patch.setattr(host._clients, "close", lambda: marker.with_suffix(".clients").touch())
        service = host._resource_service
        scope = await service.turn()
        entered = asyncio.Event()

        async def uncooperative() -> None:
            entered.set()
            while True:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    continue

        service.complete_later(scope, uncooperative(), name="uncooperative-resource")
        await entered.wait()
        try:
            await host.aclose(resource_deadline=asyncio.get_running_loop().time() + 0.02)
        finally:
            marker.write_text(str(host.resources_joined))
            await daemon.close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))

    pytest.MonkeyPatch().setattr(daemon, "run", run)
    daemon.main()


def test_unfinished_actual_task_reaches_existing_hard_exit(tmp_path: Path) -> None:
    marker = tmp_path / "join.txt"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_MACHINE_NAME=resource-join-test\n")
    env = os.environ.copy()
    env.update(
        AVA_HOME=str(home),
        RESOURCE_JOIN_MARKER=str(marker),
        AVA_MACHINE_NAME="resource-join-test",
    )
    env.pop("AVA_PERMISSIONS_HELPER_PID", None)
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and repository-owned helper
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "agent_host" / "child_runtime_scenarios.py"),
            "resource_join",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert time.monotonic() - started < 8
    assert marker.exists(), result.stdout + result.stderr
    assert marker.read_text() == "False"
    assert not marker.with_suffix(".clients").exists()
    assert "uncooperative-resource" in result.stderr
    assert "keeping pools open until hard exit" in result.stderr
