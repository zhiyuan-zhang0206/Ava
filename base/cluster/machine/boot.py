"""Compose lazy machine fields from the startup owner's explicit configuration."""

from base.cluster.machine import MachineIdentity, roles_from_flags
from base.config import ConfigBoot

__all__ = ["machine_from_boot"]


def machine_from_boot(config: ConfigBoot) -> MachineIdentity:
    """Own lazy, independently cached machine fields from this root's configuration.

    Construction reads no fields. A live command owner can reset this identity
    after repairing configuration; a delivery owner keeps its captured values.
    """
    return MachineIdentity(
        name=lambda: config.view.general.machine_name,
        role=lambda: roles_from_flags(
            serve_gateway=bool(config.view.general.machine_serve_gateway),
            serve_agent_runner=bool(config.view.general.machine_serve_agent_runner),
            serve_observability_station=bool(
                config.view.general.machine_serve_observability_station
            ),
        ),
        host=lambda: config.view.general.machine_host,
        description=lambda: config.view.general.machine_description,
    )
