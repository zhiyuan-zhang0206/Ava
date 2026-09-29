"""`ops.spec._gate_reason` must never kill the supervisor.

2026-08-08 incident: the memory-indexer plugin gate read the 'agent' config
domain from the gateway watchdog's process profile and raised AttributeError,
killing the whole watchdog on its first tick (no healthchecks ran until a
respawn without the profile). The fix: `_gate_reason` catches a raising gate,
logs, and fails OPEN (runs the service) — one plugin's gate bug must not take
the supervisor down; the capability filter already scoped the service to this
host's role.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ops.spec import ServiceSpec, _gate_reason


def _spec_with_gate(gate: Callable[[], str | None] | None) -> ServiceSpec:
    return ServiceSpec(
        session="faulty-gate",
        cmd="true",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        gate=gate,
    )


def test_gate_reason_fails_open_on_raising_gate(loguru_records: list[dict[str, Any]]) -> None:
    """A gate that raises is logged and treated as ungated (None) — never
    propagated. The rendered warning names the service and the exception: the
    record is read after loguru formats it, so a placeholder loguru does not
    fill (a printf `%s`) shows up here as a missing detail."""

    def _boom() -> str | None:
        raise RuntimeError("gate exploded")

    assert _gate_reason(_spec_with_gate(_boom)) is None
    warnings = [r["message"] for r in loguru_records if r["level"].name == "WARNING"]
    assert any("faulty-gate" in m and "gate exploded" in m for m in warnings), warnings


def test_gate_reason_passes_through_normal_gate_result() -> None:
    def _gated() -> str | None:
        return "disabled (test)"

    assert _gate_reason(_spec_with_gate(_gated)) == "disabled (test)"

    def _open() -> str | None:
        return None

    assert _gate_reason(_spec_with_gate(_open)) is None
