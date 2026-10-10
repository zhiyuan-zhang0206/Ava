"""Unrelated collectors retain global guards without consumer-local opt-in fixtures."""

import pytest


@pytest.mark.parametrize(
    "name",
    [
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
