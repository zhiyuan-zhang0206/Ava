"""Wiring hook: a supervisor whose stop deadline is short enough for a refusing child to miss."""

from typing import Any

from services.supervision.ava_root.supervisor import SupervisorConfig


def build(context: Any) -> list[object]:
    context.supervisor._config = SupervisorConfig(stop_timeout_s=0.15)
    return []
