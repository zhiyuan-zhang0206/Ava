"""Actual resource join bounds return without certifying pending clients as closed."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.tests.host_policy import configured_policy

from ... import daemon
from ...host import AgentHost


async def test_original_error_joins_before_clients_and_pools_close(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    host = AgentHost(
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine="test-box",
        catalog=model_catalog,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    scope = await host._resource_service.turn()
    failure = ValueError("original late resource error")
    host._resource_service.record_failure(scope, failure, name="original")
    events: list[str] = []
    monkeypatch.setattr(host._clients, "close", lambda: events.append("clients"))
    monkeypatch.setattr("services.agent_runner.agent_host.host.release_hosted_owner", AsyncMock())

    async def close_pools(*_args: object) -> None:
        events.append("pools")

    monkeypatch.setattr(daemon, "_close_host_pools", close_pools)
    with pytest.raises(ValueError) as observed:
        await host.aclose()
    assert observed.value is failure
    assert host.resources_joined
    await daemon._close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))
    assert events == ["clients", "pools"]


def _exercise_unfinished_main() -> None:
    """Production main's existing hard exit must skip a still-owned async task."""
    from base.deploy.schema import migrations

    def schema_gate(*_args: object) -> None:
        pass

    migrations.assert_schema_current = schema_gate

    async def run() -> None:
        host = AgentHost(
            policy=configured_policy(),
            pool=cast(Any, MagicMock()),
            checkpointer=cast(Any, object()),
            graph=cast(Any, object()),
            machine="resource-join-test",
            catalog=cast(ModelCatalog, MagicMock()),
            bus=EventBus.from_settings(),
            db=Database.from_settings(),
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
            await daemon._close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))

    daemon.run = run
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
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from services.agent_runner.agent_host.tests.runtime.test_resource_join import _exercise_unfinished_main; _exercise_unfinished_main()",
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
