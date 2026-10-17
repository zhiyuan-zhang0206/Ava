from unittest.mock import MagicMock

import pytest

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.deploy.maintenance import admission, pause_owner
from base.deploy.state import host_deploy_state
from cli.database import open_operator_database, operator_database_factory
from ops import agent_pause

__all__ = ["start_cluster_through_ready_gate"]


def start_cluster_through_ready_gate(
    monkeypatch: pytest.MonkeyPatch, *, machine: MachineIdentity
) -> None:
    database_factory = operator_database_factory(
        local_host=machine.host, open_database=open_operator_database
    )
    from base.deploy.lifecycle import start_serving
    from cli.commands.lifecycle._pause_resume import resume_after_start

    monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)

    @resume_after_start
    def ready_start(
        operation: pause_owner.PauseOwnerSnapshot | None, database_factory: DatabaseFactory
    ) -> int:
        admission.require_start_allowed(operation)
        return 0

    assert ready_start(None, database_factory, machine) == 0
    assert not admission.held()
