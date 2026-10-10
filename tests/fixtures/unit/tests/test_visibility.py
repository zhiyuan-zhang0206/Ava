"""Unrelated collectors retain global guards without consumer-local opt-in fixtures."""

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "sdk_environment",
        "add_bindings",
        "add_models",
        "config_authority",
        "fake_cancel_event",
        "default_home",
        "gateway_unit",
        "loguru_records",
        "model_catalog",
        "model_installation",
        "retry_waits",
        "runner_unit",
        "sdk_via_gateway",
        "sdk_model_owner",
        "seed_write_generation",
        "served_gateway_home",
        "serving_root",
        "set_machine_identity",
        "set_prices",
        "unit_home",
        "workspace",
    ],
)
def test_optin_fixtures_are_not_visible_to_unrelated_collectors(
    request: pytest.FixtureRequest, name: str
) -> None:
    assert not request._fixturemanager.getfixturedefs(name, request._pyfuncitem)
    assert request.config.pluginmanager.get_plugin("tests.fixtures.units") is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.unit.gateway") is None
    assert request.config.pluginmanager.get_plugin("agent.graph.llm.tests.cancel_fixture") is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.model_catalog") is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.log_capture") is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.retry_waits") is None
    assert (
        request.config.pluginmanager.get_plugin("base.deploy.lifecycle.tests.serving_root") is None
    )


def test_global_guard_cycle_does_not_load_the_sdk(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import sys
pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.guards",
]

def pytest_unconfigure(config):
    assert "ava" not in sys.modules
    assert "ava.sdk_surface.install" not in sys.modules
"""
    )
    pytester.makepyfile(
        """
import sys

def test_guarded_pure_case(request):
    assert "ava" not in sys.modules
    assert "ava.sdk_surface.install" not in sys.modules
    assert {"_restore_agent_identity", "_restore_metering", "_guard_bootstrap_fetch",
            "_guard_process_exec", "_guard_permissions_helper_native_io"} <= set(request.fixturenames)
    assert request.getfixturevalue("sdk_identity") is None
"""
    )
    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")
    result.assert_outcomes(passed=1)


def test_identity_cleanup_failure_still_restores_the_held_context(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
from types import ModuleType, SimpleNamespace
import pytest

pytest_plugins = ["tests.fixtures.identity_restore"]
sdk = ModuleType("synthetic_sdk")
held = SimpleNamespace(clients=SimpleNamespace())
sdk.context = held

@pytest.fixture
def sdk_identity():
    return sdk
"""
    )
    pytester.makepyfile(
        """
from types import SimpleNamespace
from conftest import sdk, held

class BrokenClients:
    def close(self):
        raise RuntimeError("owned-client-close-failed")

def test_1_owns_new_clients():
    sdk.context = SimpleNamespace(clients=BrokenClients())

def test_2_restored_context():
    assert sdk.context is held
"""
    )
    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")
    result.assert_outcomes(passed=2, errors=1)
    assert "owned-client-close-failed" in result.stdout.str()
