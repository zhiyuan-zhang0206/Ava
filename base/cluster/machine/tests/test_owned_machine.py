import pytest

from base.cluster.machine import MachineNameMissing
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot


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
