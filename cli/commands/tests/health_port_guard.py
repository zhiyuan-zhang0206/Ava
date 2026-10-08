"""Suite-wide health-port guard owned by the CLI probe package."""

from typing import Any, cast

import pytest


@pytest.fixture(autouse=True)
def _guard_health_port_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `ava start`'s pre-bind health-port gate finds nothing.

    The gate dials this unit's daemon `/healthz` ports before launching (issue
    #977). It does NOT reach the prod defaults (the health ports of the fixed table): the env block in
    `tests.fixtures.env_bootstrap` already pins every health port to a `_free_port()`, so an
    un-stubbed gate dials this session's own kernel-assigned ports and the suite is
    green with prod live. This fixture buys the weaker, residual property.

    `_free_port()` releases the port before anything binds it — the same race
    `tests/_containers.py` accepts for pg/redis. Nothing in a test run ever binds
    these particular ports, so the window is the whole session, and an unrelated
    process that grabs one and answers 2xx on `/healthz` would be read as
    PORT_TAKEN: a start test failing for a reason no test caused. Rare, but the
    stub costs one line and removes the flake source entirely.

    Opt out with `@pytest.mark.real_health_port_gate` when the gate itself is the
    subject (cli/commands/lifecycle/tests/startup/test_start_health_port_gate.py)."""
    # Pytest leaves FixtureRequest.node untyped; this function-scoped node is an Item.
    node = cast(pytest.Item, cast(Any, request).node)
    if node.get_closest_marker("real_health_port_gate"):
        return

    def no_occupied_ports(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr("cli.commands._probe._occupied_health_ports", no_occupied_ports)
