"""Unknown plugin gate failures abort roster evaluation instead of enabling services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ops.spec import ServiceSpec, _gate_reason


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
) -> None:
    """The operation receives the original exception and no enabled decision."""

    def _boom() -> str | None:
        raise RuntimeError("gate exploded")

    with pytest.raises(RuntimeError, match="gate exploded"):
        _gate_reason(_spec_with_gate(_boom))
    errors = [r["message"] for r in loguru_records if r["level"].name == "ERROR"]
    assert any("faulty-gate" in m and "roster evaluation aborted" in m for m in errors), errors


def test_gate_reason_passes_through_normal_gate_result() -> None:
    def _gated() -> str | None:
        return "disabled (test)"

    assert _gate_reason(_spec_with_gate(_gated)) == "disabled (test)"

    def _open() -> str | None:
        return None

    assert _gate_reason(_spec_with_gate(_open)) is None
