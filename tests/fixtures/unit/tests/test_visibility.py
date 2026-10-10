"""Unrelated test collectors retain global guards without opt-in unit fixtures."""

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "default_home",
        "gateway_unit",
        "runner_unit",
        "sdk_via_gateway",
        "seed_write_generation",
        "served_gateway_home",
        "set_machine_identity",
        "unit_home",
        "workspace",
    ],
)
def test_unit_fixtures_are_not_visible_to_unrelated_collectors(
    request: pytest.FixtureRequest, name: str
) -> None:
    assert request._fixturemanager.getfixturedefs(name, request._pyfuncitem) is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.units") is None
    assert request.config.pluginmanager.get_plugin("tests.fixtures.unit.gateway") is None
