"""Configuration slice of the computer-use MCP daemon.

Fields keep their flat registry names. `services/computer/mcp_daemon.py` builds the slice
(`computer_use_config()`, the composition root) and hands it to the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComputerUseConfig:
    computer_use_lease_s: float
    computer_use_queue_timeout_s: float
    computer_use_session_idle_s: float
    computer_use_loop_stall_s: float
    computer_use_shutdown_drain_s: float
