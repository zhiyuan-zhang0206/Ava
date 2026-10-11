"""The SDK test root shares the prepared configuration rather than booting a bare home."""

from pathlib import Path

import pytest

import ava
from base.agents.context import AvaContext
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from tests.fixtures.pin_agent import pin_agent


def test_sdk_context_uses_the_prepared_owner_with_a_bare_home(
    unit_home: Path,
    process_config: ConfigBoot,
    sdk_environment: AvaContext,
    machine_identity: MachineIdentity,
) -> None:
    assert list(unit_home.iterdir()) == []
    assert ava.context is sdk_environment
    assert ava.context.clients is sdk_environment.clients
    assert ava.context.require_machine() is machine_identity
    assert ava.self.AGENT_ID == 1
    assert process_config.view.data_plane.db_url != ""
    assert ava.context.clock_factory is not None
    clock = ava.context.clock_factory()
    assert clock.timezone == process_config.view.general.timezone
    assert clock.authoritative_timezone == (
        process_config.view.general.timezone
        if process_config.field_explicitly_set("timezone")
        else None
    )


def test_repinning_borrows_the_same_clients_and_clock(sdk_environment: AvaContext) -> None:
    pin_agent(91)
    assert ava.self.AGENT_ID == 91
    assert ava.context.clients is sdk_environment.clients
    assert ava.context.clock_factory is sdk_environment.clock_factory
    assert ava.context.machine is sdk_environment.machine


def test_the_following_case_receives_its_own_context(
    sdk_environment: AvaContext, request: pytest.FixtureRequest
) -> None:
    assert ava.context is sdk_environment
    assert ava.self.AGENT_ID == 1
    assert {"_restore_agent_identity", "_restore_metering"} <= set(request.fixturenames)


@pytest.mark.parametrize("close_fails", [False, True])
def test_sdk_root_closes_its_clients_and_restores_prior_context(
    pytester: pytest.Pytester, close_fails: bool
) -> None:
    pytester.makeconftest(
        f"""
import pytest
from tests.fixtures import env_bootstrap

pytest_plugins = ["tests.fixtures.env_bootstrap", "tests.fixtures.identity_restore"]

import ava
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity

prior = AvaContext()
ava.bind_context(prior)
closed = []
original_close = ClientSet.close


def close_owned(self, **kwargs):
    original_close(self, **kwargs)
    closed.append(self)
    if {close_fails!r}:
        raise RuntimeError("sdk-owned-close-failed")


ClientSet.close = close_owned


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    assert ava.context is prior
    assert len(closed) == 1
    return result
"""
    )
    pytester.makepyfile(
        """
import ava

def test_function_root(sdk_environment, machine_identity):
    assert ava.context is sdk_environment
    assert ava.context.require_machine() is machine_identity
"""
    )
    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")
    result.assert_outcomes(passed=1, errors=int(close_fails))
    if close_fails:
        assert "sdk-owned-close-failed" in result.stdout.str()
