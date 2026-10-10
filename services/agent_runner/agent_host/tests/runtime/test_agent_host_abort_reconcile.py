"""The aborted turn's settle-boundary reconcile — `services/agent_runner/agent_host/settlement.py`.

An aborted turn's settlement must dispose the inbounds that turn claimed, and
every gap must skip the pass and leave them to the next cold admission: the
soft switch off, an unresolved turn resource, a replaced runtime, and any
failure of the pass itself. The DB-visible split it performs is locked in
`services/agent_runner/agent_host/recovery/tests/test_reconcile_after_abort.py`; the settlement trigger that
dispatches it is locked in `test_agent_host.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.ownership.inbound import RuntimeOwnershipLostError
from base.config import settings
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host import settlement as settlement_mod
from services.agent_runner.agent_host.tests.host_policy import configured_policy


class _ReconcileSpy:
    """Records each pass, the explicit original incarnation, and fails on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, int]] = []
        self.bound: list[RuntimeIncarnation | None] = []
        self.fail_with: BaseException | None = None

    async def __call__(
        self,
        pool: object,
        checkpointer: object,
        agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        inputs: object,
    ) -> None:
        self.calls.append((pool, checkpointer, agent_id))
        self.bound.append(incarnation)
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture
def reconcile_spy(monkeypatch: pytest.MonkeyPatch) -> _ReconcileSpy:
    spy = _ReconcileSpy()
    monkeypatch.setattr(settlement_mod, "reconcile_claimed_inbounds_at_startup", spy)
    return spy


def _incarnation(agent_id: int = 42) -> RuntimeIncarnation:
    return RuntimeIncarnation(agent_id, uuid4(), uuid4())


async def _run_pass(
    incarnation: RuntimeIncarnation, *, resources: HostedTurnResources | None = None
) -> None:
    """Invoke the pass with sentinel handles — only the patched spy sees them."""
    await settlement_mod.reconcile_inbounds_after_abort(
        cast(AsyncConnectionPool[Any], object()),
        cast(AsyncPostgresSaver, object()),
        incarnation,
        resources=resources,
        inputs=configured_policy().reconcile_inputs,
    )


def _skips(loguru_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in loguru_records if r["extra"].get("event") == "host_abort_reconcile_skipped"]


async def test_pass_runs_under_the_aborts_own_incarnation(reconcile_spy: _ReconcileSpy) -> None:
    """The helper must see the abort's incarnation — the inbound-owner lock
    fences its writes on that lease — and the caller's pool/checkpointer."""
    incarnation = _incarnation()
    await _run_pass(incarnation)
    assert len(reconcile_spy.calls) == 1
    assert reconcile_spy.calls[0][2] == 42
    assert reconcile_spy.bound == [incarnation]


async def test_disabled_soft_switch_skips(
    reconcile_spy: _ReconcileSpy,
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.daemon, "host_abort_reconcile_enabled", False)
    await _run_pass(_incarnation())
    assert reconcile_spy.calls == []
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["disabled"]


async def test_unresolved_turn_resources_skip(
    reconcile_spy: _ReconcileSpy,
    loguru_records: list[dict[str, Any]],
) -> None:
    """An unresolved host resource is a turn that has not settled: the pass
    must not decide which claimed rows are durable while the turn can still
    discharge them, and the boot reconcile stays the fallback."""
    resources = HostedTurnResources()
    resources.unresolved[Path("hosted-resource/still-held")] = None
    await _run_pass(_incarnation(), resources=resources)
    assert reconcile_spy.calls == []
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["resources_unsettled"]


async def test_replaced_runtime_is_a_logged_noop(
    reconcile_spy: _ReconcileSpy,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A stale incarnation's pass must neither write nor raise: the lease
    fence refuses it and the settlement continues as if it had not run."""
    reconcile_spy.fail_with = RuntimeOwnershipLostError("runtime replaced")
    await _run_pass(_incarnation())
    assert len(reconcile_spy.calls) == 1
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["ownership_lost"]


async def test_unexpected_failure_is_logged_not_raised(
    reconcile_spy: _ReconcileSpy,
    loguru_records: list[dict[str, Any]],
) -> None:
    """The pass is best-effort: a database failure must not become a second
    turn failure — the next cold admission retries the reconcile."""
    reconcile_spy.fail_with = RuntimeError("database is down")
    await _run_pass(_incarnation())
    failed = [r for r in loguru_records if r["extra"].get("event") == "host_abort_reconcile_failed"]
    assert [r["extra"]["agent_id"] for r in failed] == [42]
    assert failed[0]["exception"] is not None  # the traceback rides the record (#4964)
    assert failed[0]["exception"].type is RuntimeError
