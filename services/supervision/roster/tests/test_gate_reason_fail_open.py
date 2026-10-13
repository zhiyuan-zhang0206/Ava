"""Unknown plugin gate failures abort roster evaluation instead of enabling services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.cluster.machine import MachineIdentity
from services.supervision.roster.spec import ServiceSpec, gate_reason
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _spec_with_gate(gate: Callable[[], str | None] | None) -> ServiceSpec:
    return ServiceSpec(
        session="faulty-gate",
        cmd="true",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        gate=gate,
    )


def test_gate_reason_reports_and_propagates_raising_gate(
    loguru_records: list[dict[str, Any]],
    machine_identity: MachineIdentity,
) -> None:
    """The operation receives the original exception and no enabled decision."""

    def _boom() -> str | None:
        raise RuntimeError("gate exploded")

    with pytest.raises(RuntimeError, match="gate exploded"):
        gate_reason(_spec_with_gate(_boom), machine=machine_identity)
    errors = [r["message"] for r in loguru_records if r["level"].name == "ERROR"]
    assert any("faulty-gate" in m and "roster evaluation aborted" in m for m in errors), errors


def test_gate_reason_passes_through_normal_gate_result(machine_identity: MachineIdentity) -> None:
    def _gated() -> str | None:
        return "disabled (test)"

    assert gate_reason(_spec_with_gate(_gated), machine=machine_identity) == "disabled (test)"

    def _open() -> str | None:
        return None

    assert gate_reason(_spec_with_gate(_open), machine=machine_identity) is None
