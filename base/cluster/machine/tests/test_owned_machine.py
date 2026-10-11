"""Composition-owned machine fields keep lazy caches and explicit reset boundaries."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.cluster.machine import MachineNameMissing
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot


def test_machine_value_and_pipeline_type_imports_do_not_load_settings(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])

class SettingsForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "base.config" or fullname.startswith("base.config."):
            raise AssertionError("value imports must not load settings")
        return None

sys.meta_path.insert(0, SettingsForbidden())
from base.cluster.machine import MachineIdentity
from base.telemetry import EventPipeline
assert "base.config" not in sys.modules
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)
    result = subprocess.run(  # noqa: S603 — fixed interpreter and code, isolated temporary home
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[4])],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_construction_does_not_read_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ConfigBoot()

    def refused(_self: ConfigBoot, _name: str) -> object:
        pytest.fail("construction must not read machine configuration")

    monkeypatch.setattr(ConfigBoot, "get_field", refused)
    machine_from_boot(config)


def test_role_never_requires_name_and_delivery_owners_are_independent() -> None:
    prior_config = ConfigBoot()
    prior_config.read_process_environment()
    prior_config.set_field("machine_name", "")
    prior_config.set_field("machine_serve_gateway", True)
    prior_config.set_field("machine_serve_agent_runner", False)
    prior_config.set_field("machine_serve_observability_station", False)
    prior = machine_from_boot(prior_config)
    assert prior.role() == frozenset({"gateway"})
    with pytest.raises(MachineNameMissing):
        prior.name()
    next_config = ConfigBoot()
    next_config.read_process_environment()
    next_config.set_field("machine_name", "next-host")
    current = machine_from_boot(next_config)
    assert current.name() == "next-host"
    prior.reset()
    with pytest.raises(MachineNameMissing):
        prior.name()
    assert current.name() == "next-host"


def test_owner_reset_observes_repaired_fields_without_resetting_another() -> None:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.general.machine_name = "before"
    config.view.general.machine_description = "first"
    machine = machine_from_boot(config)
    other = machine_from_boot(config)
    assert machine.name() == other.name() == "before"
    assert machine.description() == "first"
    config.view.general.machine_name = "after"
    config.view.general.machine_description = "second"
    assert machine.name() == "before"
    machine.reset()
    assert machine.name() == "after"
    assert machine.description() == "second"
    assert other.name() == "before"
