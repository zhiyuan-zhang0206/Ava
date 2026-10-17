from typing import Any

from services.supervision.meiva_root.supervisor import SupervisorConfig


def build(context: Any) -> list[object]:
    context.supervisor._config = SupervisorConfig(stop_timeout_s=0.15)
    return []
