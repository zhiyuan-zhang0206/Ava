"""The bounded release of an abandoned pre-stop maintenance hold (task #3270).

Covers the release action (`ops.strand_hold.maybe_release_abandoned_hold`) and
the cancel half it runs (`ops.cluster_pause.release_pre_stop_hold`): the window,
the re-verified proof under the lock, and the refusal parity with the
operator's `ava maintenance resume --cancel`.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest

from ops import strand_hold
from shared import pause_owner
from shared.maintenance_state import MaintenanceHold

_AT = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)


def _snapshot(
    phase: str = "draining", *, failures: dict[int, str] | None = None
) -> pause_owner.PauseOwnerSnapshot:
    return pause_owner.PauseOwnerSnapshot(
        status="paused",
        holder="op-1",
        acquired_at=_AT,
        maintenance=MaintenanceHold(phase, failures=dict(failures or {})),  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _lifecycle_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import ui_update_state

    monkeypatch.setattr(ui_update_state, "lifecycle_lock", contextlib.nullcontext)


@pytest.fixture
def releases(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _record(*, reason: str) -> None:
        calls.append(reason)

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _record)
    return calls


def test_below_the_window_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, releases: list[str]
) -> None:
    monkeypatch.setattr(pause_owner, "read", _snapshot)
    strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S - 1)
    assert releases == []


def test_the_toggle_disables_the_release(
    monkeypatch: pytest.MonkeyPatch, releases: list[str]
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "abandoned_hold_auto_release", False)
    monkeypatch.setattr(pause_owner, "read", _snapshot)
    strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S + 1)
    assert releases == []


def test_past_the_window_the_release_runs(
    monkeypatch: pytest.MonkeyPatch, releases: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pause_owner, "read", _snapshot)

    def _dead(_current: object) -> str:
        return "dead"

    monkeypatch.setattr(strand_hold, "driver_reading", _dead)

    def _no_owner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("ops.controllers.stranded_pause._executing_owner", _no_owner)
    with caplog.at_level("ERROR"):
        strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S + 1)
    assert releases == ["watchdog: abandoned pre-stop hold auto-release"]
    assert any("auto-released an abandoned pre-stop hold" in r.message for r in caplog.records)


def test_a_returned_shepherd_skips_the_release(
    monkeypatch: pytest.MonkeyPatch, releases: list[str]
) -> None:
    monkeypatch.setattr(pause_owner, "read", _snapshot)

    def _alive(_current: object) -> str:
        return "alive"

    monkeypatch.setattr(strand_hold, "driver_reading", _alive)
    strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S + 1)
    assert releases == []


def test_a_new_executing_owner_skips_the_release(
    monkeypatch: pytest.MonkeyPatch, releases: list[str]
) -> None:
    monkeypatch.setattr(pause_owner, "read", _snapshot)

    def _dead(_current: object) -> str:
        return "dead"

    monkeypatch.setattr(strand_hold, "driver_reading", _dead)

    def _owner(*_args: object, **_kwargs: object) -> str:
        return "an update is executing"

    monkeypatch.setattr("ops.controllers.stranded_pause._executing_owner", _owner)
    strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S + 1)
    assert releases == []


def test_a_failed_release_is_loud_and_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import shared.host_deploy_state as hds

    monkeypatch.setattr(pause_owner, "read", _snapshot)

    def _dead(_current: object) -> str:
        return "dead"

    monkeypatch.setattr(strand_hold, "driver_reading", _dead)

    def _no_owner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("ops.controllers.stranded_pause._executing_owner", _no_owner)

    def _boom(*, reason: str) -> None:
        raise RuntimeError("dependencies unavailable")

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _boom)
    marked: list[str] = []

    def _mark(reason: str) -> bool:
        marked.append(reason)
        return True

    monkeypatch.setattr(hds, "mark_stranded_hold", _mark)
    with caplog.at_level("ERROR"):
        strand_hold.maybe_release_abandoned_hold(paused_for=strand_hold.AUTO_RELEASE_S + 1)
    assert len(marked) == 1 and "auto-release failed" in marked[0]
    assert any("automatic release attempt failed" in r.message for r in caplog.records)


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, *_args: object) -> None:
        return None


def _release_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: pause_owner.PauseOwnerSnapshot | None,
    role: frozenset[str] = frozenset(),
) -> list[bool]:
    from ops import cluster_pause
    from shared import db as db_mod
    from shared import maintenance as maintenance_mod

    monkeypatch.setattr(maintenance_mod, "snapshot", lambda: snapshot)
    monkeypatch.setattr("shared.machine.machine_role", lambda: role)
    monkeypatch.setattr(db_mod, "connect", _FakeConn)
    unpaused: list[bool] = []
    monkeypatch.setattr(cluster_pause, "unpause_local_cluster", lambda: unpaused.append(True))
    return unpaused


def test_release_refuses_without_a_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops import cluster_pause

    _release_env(monkeypatch, snapshot=None)
    with pytest.raises(RuntimeError, match="no maintenance hold stands"):
        cluster_pause.release_pre_stop_hold(reason="test")


def test_release_refuses_a_started_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops import cluster_pause

    _release_env(monkeypatch, snapshot=_snapshot("stopping"))
    with pytest.raises(RuntimeError, match="cancel cannot bypass a started stop"):
        cluster_pause.release_pre_stop_hold(reason="test")


def test_release_refuses_failed_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops import cluster_pause

    _release_env(monkeypatch, snapshot=_snapshot("drained", failures={3: "checkpoint_flush"}))
    with pytest.raises(RuntimeError, match="failed continuation/flush receipts"):
        cluster_pause.release_pre_stop_hold(reason="test")


def test_release_runs_the_cancel_sequence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from ops import cluster_pause

    unpaused = _release_env(monkeypatch, snapshot=_snapshot("draining"))
    with caplog.at_level("ERROR"):
        cluster_pause.release_pre_stop_hold(reason="watchdog test")
    assert unpaused == [True]
    assert any(
        "released a pre-stop maintenance hold (watchdog test)" in r.message for r in caplog.records
    )


def test_release_proves_the_runner_host_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cancel mirrors the operator verb: a runner's live agent-host must
    answer before the hold is released."""
    from ops import cluster_pause

    unpaused = _release_env(
        monkeypatch, snapshot=_snapshot("drained"), role=frozenset({"agent-runner"})
    )

    def _refuse() -> object:
        raise RuntimeError("agent-host does not support maintenance for this home")

    monkeypatch.setattr("ops.agent_pause_probe.host_identity", _refuse)
    with pytest.raises(RuntimeError, match="does not support maintenance"):
        cluster_pause.release_pre_stop_hold(reason="test")
    assert unpaused == []


def test_release_leaves_the_hold_when_the_data_plane_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops import cluster_pause
    from shared import db as db_mod

    unpaused = _release_env(monkeypatch, snapshot=_snapshot("drained"))

    def _unreachable() -> object:
        raise ConnectionError("db down")

    monkeypatch.setattr(db_mod, "connect", _unreachable)
    with pytest.raises(ConnectionError, match="db down"):
        cluster_pause.release_pre_stop_hold(reason="test")
    assert unpaused == []
