"""The real local stats HTTP route and registered health-port lookup agree."""

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from cli.commands._maintenance_probe import host_identity
from services.agent_host.daemon import _stats_route
from shared.config import settings
from shared.daemon_health import start_health_server, stop_health_server


async def test_actual_stats_route_matches_configured_port_home_pid_and_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    pidfile = tmp_path / "agent-host.pid"
    pidfile.write_text(str(os.getpid()))
    monkeypatch.setattr(settings.services, "agent_host_pidfile", pidfile)
    host, scheduler = MagicMock(), MagicMock()
    host._owner = uuid4()
    host.stats.as_payload.return_value = {}
    scheduler.active_agents = {42}
    server = await start_health_server(
        "agent_host",
        port=0,
        extra_routes={("GET", "/stats"): _stats_route(host, scheduler)},
    )
    monkeypatch.setattr(
        settings.services, "agent_host_health_port", server.sockets[0].getsockname()[1]
    )
    try:
        identity = await asyncio.to_thread(host_identity)
        assert identity.owner == host._owner
        assert identity.active == frozenset({42})
        pidfile.write_text(str(os.getpid() + 1))
        with pytest.raises(RuntimeError, match="pidfile"):
            await asyncio.to_thread(host_identity)
    finally:
        await stop_health_server(server)
