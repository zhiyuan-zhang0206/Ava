"""CLI orchestration guards apply beside their actual consumers."""

import pytest

from cli.commands.tests.health_port_guard import guard_health_port_gate as guard_health_port_gate


@pytest.fixture(autouse=True)
def _cli_orchestration_guards(
    guard_health_port_gate: None,
    guard_service_readiness: None,
    homes_get_their_own_ports: None,
    permissions_helper_native_io: None,
) -> None:
    """Admit CLI logic while keeping probes and helper commands off the host."""
