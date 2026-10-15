from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity


def test_gateway_unit_is_gateway(
    gateway_unit: TestClient, machine_identity: MachineIdentity
) -> None:
    assert machine_identity.role() == frozenset({"gateway"})
    assert cast(FastAPI, gateway_unit.app).state.machine is machine_identity
    assert isinstance(gateway_unit, TestClient)


def test_runner_unit_is_agent_runner(runner_unit: MachineIdentity) -> None:
    assert runner_unit.role() == frozenset({"agent-runner"})


def test_role_restored_after_gateway_unit(machine_identity: MachineIdentity) -> None:
    assert machine_identity.role() == frozenset({"agent-runner"})
