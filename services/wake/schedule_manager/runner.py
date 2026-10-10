"""SDK composition and CLI entrypoint for an independent schedule process."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

import ava
from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.paths import prod_service_checkout_error
from gateway.schedules import runner as engine


def _bind_schedule_actor(schedule_id: int) -> None:
    """Attribute SDK operations to this schedule using the process's client owner."""
    ava.bind_context(
        AvaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=process_context.process_clients(),
        )
    )


def run(schedule_id: int, revision: int | None = None) -> int:
    """Run a schedule with the SDK policy inputs owned by this process."""
    return engine.run(
        schedule_id,
        revision,
        bind_actor=_bind_schedule_actor,
        load_plugins=ava.ensure_plugins_loaded,
    )


def main() -> None:
    if len(sys.argv) not in (2, 3):
        logger.error(
            "Usage: python -m services.wake.schedule_manager.runner <schedule_id> [revision]"
        )
        raise SystemExit(2)
    # The runner's checkout anchors every subprocess it spawns. Refuse a dev
    # checkout against the production home before reading or executing a script.
    refusal = prod_service_checkout_error(Path(__file__).resolve().parents[3])
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    revision = int(sys.argv[2]) if len(sys.argv) == 3 else None
    if revision is not None and revision < 0:
        raise SystemExit(2)
    raise SystemExit(run(int(sys.argv[1]), revision))


if __name__ == "__main__":
    main()
