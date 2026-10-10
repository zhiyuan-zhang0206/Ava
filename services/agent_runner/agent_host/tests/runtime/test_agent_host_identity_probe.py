"""The real local stats HTTP route and registered health-port lookup agree."""

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from base.agents.observation.turn_progress import TurnProgress
from base.config import settings
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from ops.agent_pause.probe import host_identity
from services.agent_runner.agent_host.scheduling.health_routes import stats_route


async def test_actual_stats_route_matches_configured_port_home_pid_and_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    pidfile = ServiceEndpoints.from_settings().of("agent_host").pidfile
    pidfile.write_text(str(os.getpid()))
    host, scheduler = MagicMock(), MagicMock()
    host.runtime_owner = uuid4()
    host.turn_progress = TurnProgress()
    host.stats.as_payload.return_value = {}
    scheduler.active_agents = {42}
    server = await start_health_server(
        "agent_host",
        port=0,
        extra_routes={("GET", "/stats"): stats_route(host, scheduler)},
    )
    monkeypatch.setattr(
        settings.services, "agent_host_health_port", server.sockets[0].getsockname()[1]
    )
    try:
        identity = await asyncio.to_thread(host_identity)
        assert identity.owner == host.runtime_owner
        assert identity.active == frozenset({42})
        pidfile.write_text(str(os.getpid() + 1))
        with pytest.raises(RuntimeError, match="pidfile"):
            await asyncio.to_thread(host_identity)
    finally:
        await stop_health_server(server)
