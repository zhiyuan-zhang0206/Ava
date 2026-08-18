"""Smoke tests for the two-unit fixtures (gateway_unit / runner_unit).

Proves the fixtures switch machine_role at its source so every call site of
`machine_role()` agrees, without per-module monkeypatching.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_gateway_unit_is_gateway(gateway_unit: TestClient) -> None:
    from shared.machine import machine_role

    assert machine_role() == frozenset({"gateway"})
    # the yielded value is a usable gateway TestClient
    assert isinstance(gateway_unit, TestClient)


def test_runner_unit_is_agent_runner(runner_unit: None) -> None:
    from shared.machine import machine_role

    assert machine_role() == frozenset({"agent-runner"})


def test_role_restored_after_gateway_unit() -> None:
    # session default is agent-runner; after a gateway_unit test tore down,
    # the cache was cleared so the default resolves again.
    from shared.machine import machine_role

    assert machine_role() == frozenset({"agent-runner"})
