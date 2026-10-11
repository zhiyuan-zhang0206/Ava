"""One invocation owns lazy identity caches and function-local configuration."""

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_machine as command_machine
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime


def test_root_construction_does_not_prepare_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_prepare(_config: ConfigBoot) -> None:
        pytest.fail("constructing an invocation delivered configuration")

    monkeypatch.setattr(ConfigBoot, "prepare", unexpected_prepare)
    first = CommandRuntime.create()
    second = CommandRuntime.create()
    assert first.config is not second.config
    assert first.machine is not second.machine
    assert not first.config.prepared and not second.config.prepared


def test_reset_reloads_only_this_invocations_identity(command_runtime: CommandRuntime) -> None:
    other = CommandRuntime.create()
    other.config.set_field("machine_name", "other-root")
    command_runtime.config.set_field("machine_name", "first-name")
    assert command_runtime.machine.name() == "first-name"
    assert other.machine.name() == "other-root"

    command_runtime.config.set_field("machine_name", "repaired-name")
    assert command_runtime.machine.name() == "first-name"
    command_runtime.machine.reset()
    assert command_runtime.machine.name() == "repaired-name"
    assert other.machine.name() == "other-root"


def test_narrow_machine_fixture_borrows_the_invocation_owner(
    command_runtime: CommandRuntime, command_machine: MachineIdentity
) -> None:
    assert command_machine is command_runtime.machine
