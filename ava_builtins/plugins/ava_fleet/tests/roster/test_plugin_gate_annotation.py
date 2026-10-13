"""A plugin service's own gate flows through the annotated roster view.

Split out of `services/supervision/roster/tests/test_plugin_services.py`: it writes the fleet plugin's real `FleetConfig` disk image, so it lives with the fleet plugin."""

from __future__ import annotations

import pytest

from base.cluster.machine import MachineIdentity
from services.supervision.roster import spec
from tests.fixtures.unit.identity import machine_identity as machine_identity


def test_plugin_gate_flows_through_annotation(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """The plugin service's own gate is honored by ops: disabling task-maintenance
    drops it from the start roster but keeps it (with a reason) in the annotated
    view — same contract as the core config-gated services."""
    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from base.packages.plugins.config_registration import disk_image_path

    image = disk_image_path("ava_fleet")
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(FleetConfig(task_maintenance_enabled=False).model_dump_json())
    start = {
        s.session
        for s in spec.services_for_capabilities(frozenset({"gateway"}), machine=machine_identity)
    }
    assert "task-maintenance" not in start
    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert annotated["task-maintenance"] and "disabled" in annotated["task-maintenance"]
