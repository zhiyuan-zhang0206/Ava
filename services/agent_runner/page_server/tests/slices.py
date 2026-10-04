"""The page-server slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.agent_runner.page_server import daemon
from services.agent_runner.page_server.config import PageServerConfig


def page_server_config(**overrides: Any) -> PageServerConfig:
    return replace(daemon.page_server_config(), **overrides)
