"""The clock lattice registers the agent host's actual liveness beat."""

from __future__ import annotations

from base.deploy.timing import CLOCKS


def test_renewal_clock_is_the_agent_hosts_actual_beat() -> None:
    """The lattice must check the renewal beat that runs, not a second number
    (it once registered 60 s while the agent host renewed every 15 s)."""
    from services.agent_runner.agent_host import daemon as agent_host_daemon

    assert CLOCKS["AGENT_LEASE_RENEW_INTERVAL_S"].get() == agent_host_daemon._LIVENESS_BEAT_STEP_S
