import os
from pathlib import Path
from typing import Any

from services.supervision.meiva_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.supervision.meiva_root.supervisor import SupervisorConfig


class Monitor(TreeSelfCheck):
    def run_once(self) -> None:
        if Path(os.environ["MEIVA_ROOT_WIRING_FAULT"]).exists():
            raise ValueError("unknown-monitor-primary")


def build(context: Any) -> Monitor:
    context.supervisor._config = SupervisorConfig(stop_timeout_s=0.15)
    return Monitor(
        context.supervisor,
        config=SelfCheckConfig(interval_s=0.01),
        tasks=context.participant_tasks,
    )
