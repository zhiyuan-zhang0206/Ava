"""The computer-use slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.computer import mcp_daemon
from services.computer.config import ComputerUseConfig


def computer_use_config(**overrides: Any) -> ComputerUseConfig:
    return replace(mcp_daemon.computer_use_config(), **overrides)
