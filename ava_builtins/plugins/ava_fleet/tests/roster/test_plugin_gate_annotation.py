from __future__ import annotations

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.tests.fake_commands import fake_host
from services.supervision.roster import spec


def test_plugin_gate_flows_through_annotation(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from base.packages.plugins.config_registration import disk_image_path

    image = disk_image_path("ava_fleet")
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(FleetConfig(task_maintenance_enabled=False).model_dump_json())
    start = {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({"gateway"}), machine=machine_identity, host=fake_host()
        )
    }
    assert "task-maintenance" not in start
    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity, host=fake_host()
        )
    }
    assert annotated["task-maintenance"] and "disabled" in annotated["task-maintenance"]
