# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""The retention deletion state machine: arm gating, stability, double-run.

These lock the P1c contract (design v0.3 section 3): the deletion tick is a
no-op until the operator arm carrier and the approved digest are both present,
the digest must hold for consecutive ticks, the first execution of a process
recomputes the plan before deleting, and every executed tick goes through the
bounded executor with the approved digest.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from services.pitr import retention_scheduler
from services.pitr.retention_executor import RetentionExecutionSummary
from services.pitr.retention_planner import DryRunResult
from services.pitr.retention_scheduler import (
    RetentionDryRunState,
    delete_tick,
    health_component,
)

DIGEST = "digest-1"


def _result(*, digest: str = DIGEST, blocked: bool = False) -> DryRunResult:
    return DryRunResult(
        path=Path("plan.json"),
        digest=digest,
        blocked=blocked,
        retained_objects=1,
        eligible_objects=2,
        retained_bytes=10,
        eligible_bytes=20,
        remote_object_count=3,
        remote_bytes=100,
    )


def _summary(**overrides: Any) -> RetentionExecutionSummary:
    fields: dict[str, Any] = {
        "plan_digest": DIGEST,
        "refused_reason": None,
        "attempted": 3,
        "deleted": 2,
        "absent": 1,
        "mismatched": 0,
        "failed": 0,
        "verify_failed": 0,
        "skipped": 0,
    }
    fields.update(overrides)
    return RetentionExecutionSummary(**fields)


class _FakeViewer:
    def stat(self, object_name: str) -> object:
        return None


class _FakeGroup:
    def __init__(self) -> None:
        self.delete_store = object()

    def retention_inventory_reader(self) -> object:
        return object()

    def viewer_object_store(self) -> _FakeViewer:
        return _FakeViewer()

    def retention_delete_store(self) -> object:
        return self.delete_store


class _SilentTelemetry:
    def emit(self, *args: object, **kwargs: object) -> None:
        return None


def _arm(monkeypatch: pytest.MonkeyPatch, *, armed: bool, digest: str | None = DIGEST) -> None:
    monkeypatch.setattr(retention_scheduler, "_live_delete_armed", lambda **_kw: armed)
    monkeypatch.setattr(retention_scheduler, "_live_approved_digest", lambda **_kw: digest)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the module's seams with recording fakes."""
    calls: dict[str, Any] = {"execute": [], "plans": 0, "group": _FakeGroup()}
    monkeypatch.setattr(retention_scheduler, "get_store_group", lambda: calls["group"])
    monkeypatch.setattr(
        retention_scheduler, "_build_verify_absent", lambda *_args, **_kw: lambda _name: True
    )
    monkeypatch.setattr(retention_scheduler, "inspect_dry_run_plan", lambda _root: "plan")
    monkeypatch.setattr(retention_scheduler, "telemetry", _SilentTelemetry())

    def fake_write(
        root: Path, *, retain_chains: int = 2, inventory_reader: object = None
    ) -> DryRunResult:
        calls["plans"] += 1
        return _result(digest=calls.get("write_digest", DIGEST))

    def fake_execute(plan: object, **kwargs: object) -> RetentionExecutionSummary:
        calls["execute"].append((plan, kwargs))
        return calls.get("summary") or _summary()

    monkeypatch.setattr(retention_scheduler, "write_dry_run_plan", fake_write)
    monkeypatch.setattr(retention_scheduler, "execute_retention_plan", fake_execute)
    return calls


def _config() -> Any:
    from shared.config import settings

    return settings.physical_backup


def test_disabled_planner_reports_disabled(wired: dict[str, Any]) -> None:
    state = RetentionDryRunState(enabled=False)
    delete_tick(state, _config())
    assert state.delete.status == "disabled"
    assert state.delete.armed is False
    assert wired["plans"] == 0
    assert wired["execute"] == []


def test_unarmed_tick_stays_dry_run(wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch, armed=False)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    assert state.delete.status == "dry-run"
    assert wired["execute"] == []


def test_blocked_plan_holds_deletion(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result(blocked=True))
    delete_tick(state, _config())
    assert state.delete.status == "dry-run"
    assert "blockers" in (state.delete.last_error or "")
    assert wired["execute"] == []


def test_digest_mismatch_holds_deletion(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True, digest="approved-other")
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    assert state.delete.status == "dry-run"
    assert "approved" in (state.delete.last_error or "")
    assert wired["execute"] == []


def test_missing_approved_digest_holds_deletion(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True, digest=None)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    assert state.delete.status == "dry-run"
    assert "approved" in (state.delete.last_error or "")


def test_stable_digest_arms_and_executes(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())  # first tick counts stability only
    assert state.delete.status == "dry-run"
    assert state.delete.stable_count == 1
    assert wired["execute"] == []
    delete_tick(state, _config())  # second tick: stable -> execute
    assert state.delete.status == "armed"
    assert state.delete.stable_count == 2
    assert len(wired["execute"]) == 1
    _plan, kwargs = wired["execute"][0]
    assert kwargs["expected_digest"] == DIGEST
    assert kwargs["remote_total_bytes"] == 100
    assert kwargs["delete_store"] is wired["group"].delete_store
    assert "journal" in kwargs


def test_digest_change_resets_stability(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result(digest="digest-1"))
    delete_tick(state, _config())
    assert state.delete.stable_count == 1
    state.plan = _result(digest="digest-2")
    _arm(monkeypatch, armed=True, digest="digest-2")
    wired["write_digest"] = "digest-2"
    delete_tick(state, _config())
    assert state.delete.stable_count == 1  # reset to the new digest, not accumulated
    assert wired["execute"] == []
    delete_tick(state, _config())
    assert state.delete.stable_count == 2
    assert len(wired["execute"]) == 1


def test_first_execution_double_run_refuses_changed_plan(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    wired["write_digest"] = "changed"
    delete_tick(state, _config())
    assert wired["execute"] == []
    assert "recheck" in (state.delete.last_error or "")
    assert state.delete.status == "armed"


def test_double_run_only_once_per_process(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    delete_tick(state, _config())  # executes once, one recheck
    assert wired["plans"] == 1
    delete_tick(state, _config())  # next execution tick: no recheck
    assert wired["plans"] == 1
    assert len(wired["execute"]) == 2


def test_totals_accumulate_from_summaries(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    wired["summary"] = _summary(
        deleted=2, sidecars_deleted=1, orphans_deleted=1, absent=1, failed=0
    )
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())
    delete_tick(state, _config())
    totals = state.delete.totals
    assert totals.ticks == 1
    assert totals.deleted == 4
    assert totals.absent == 1
    assert totals.failed == 0


def test_execution_error_is_recorded_and_state_stays_armed(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, armed=True)
    state = RetentionDryRunState(enabled=True, plan=_result())
    delete_tick(state, _config())

    def boom(plan: object, **kwargs: object) -> RetentionExecutionSummary:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(retention_scheduler, "execute_retention_plan", boom)
    delete_tick(state, _config())
    assert state.delete.status == "armed"
    assert "store exploded" in (state.delete.last_error or "")
    assert state.delete.totals.ticks == 0


def test_health_reports_armed_deletion() -> None:
    state = RetentionDryRunState(enabled=True, plan=_result(), last_success=time.time())
    state.delete.status = "armed"
    state.delete.armed = True
    state.delete.armed_at = 123.0
    record = health_component(state)
    assert record["delete_enabled"] is True
    assert record["delete_state"] == "armed"
    assert record["armed_at"] == 123.0
    assert record["delete_totals"] == {"ticks": 0, "deleted": 0, "absent": 0, "failed": 0}


def test_health_disables_delete_by_default() -> None:
    state = RetentionDryRunState(enabled=True, plan=_result(), last_success=time.time())
    record = health_component(state)
    assert record["delete_enabled"] is False
    assert record["delete_state"] == "disabled"


def test_live_armed_reads_the_carrier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: "true")
    assert retention_scheduler._live_delete_armed(boot_value=False) is True
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: "false")
    assert retention_scheduler._live_delete_armed(boot_value=True) is False
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: None)
    assert retention_scheduler._live_delete_armed(boot_value=True) is True


def test_live_approved_digest_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: "  abc  ")
    assert retention_scheduler._live_approved_digest(boot_value=None) == "abc"
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: "   ")
    assert retention_scheduler._live_approved_digest(boot_value="boot") is None
    monkeypatch.setattr(retention_scheduler, "_read_carrier", lambda _alias: None)
    assert retention_scheduler._live_approved_digest(boot_value="boot") == "boot"


def test_build_verify_absent_uses_the_viewer_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Viewer:
        def stat(self, object_name: str) -> object:
            return None

    class _Group:
        def viewer_object_store(self) -> _Viewer:
            return _Viewer()

    from shared.config import settings

    monkeypatch.setattr(retention_scheduler, "get_store_group", _Group)
    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_store_backend", "oss")
    probe = retention_scheduler._build_verify_absent(config)
    assert probe("any") is True


def test_build_verify_absent_polls_for_baidu(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    class _Viewer:
        def __init__(self, *, absent_after: int | None) -> None:
            self.calls = 0
            self.absent_after = absent_after

        def stat(self, object_name: str) -> object:
            self.calls += 1
            if self.absent_after is not None and self.calls >= self.absent_after:
                return None
            return object()

    class _Group:
        def __init__(self, viewer: _Viewer) -> None:
            self.viewer = viewer

        def viewer_object_store(self) -> _Viewer:
            return self.viewer

    from shared.config import settings

    monkeypatch.setattr(
        retention_scheduler,
        "time",
        types.SimpleNamespace(sleep=lambda _seconds: None, time=time.time),
    )
    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_store_backend", "baidu")

    viewer = _Viewer(absent_after=3)
    monkeypatch.setattr(retention_scheduler, "get_store_group", lambda: _Group(viewer))
    assert retention_scheduler._build_verify_absent(config)("any") is True
    assert viewer.calls == 3

    sticky = _Viewer(absent_after=None)
    monkeypatch.setattr(retention_scheduler, "get_store_group", lambda: _Group(sticky))
    assert retention_scheduler._build_verify_absent(config)("any") is False
    assert sticky.calls == 5
