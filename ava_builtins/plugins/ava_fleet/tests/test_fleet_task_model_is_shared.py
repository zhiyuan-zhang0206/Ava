"""The fleet plugin's task model is the shared task model.

Split out of `ops/agents/tests/test_creation_transactions.py`."""

from base.agents.tasks.model import Task
from tests.fixtures.unit.identity import machine_identity as machine_identity


def test_fleet_task_model_remains_the_shared_model() -> None:
    from ava_builtins.plugins.ava_fleet.task_registry import Task as FleetTask

    assert FleetTask is Task
