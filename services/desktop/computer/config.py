from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComputerUseConfig:
    computer_use_lease_s: float
    computer_use_queue_timeout_s: float
    computer_use_session_idle_s: float
    computer_use_loop_stall_s: float
    computer_use_shutdown_drain_s: float
