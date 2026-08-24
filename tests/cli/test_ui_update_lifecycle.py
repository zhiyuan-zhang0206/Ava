# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""The detached gateway orchestration owns exactly one persisted UI generation."""

from pathlib import Path

import pytest

from cli.commands import update
from shared import ui_update_state


@pytest.fixture(autouse=True)
def _isolated_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stub_deploy_lease_identity: None,
) -> None:
    monkeypatch.setattr(ui_update_state, "state_path", lambda: tmp_path / "deploy-state.json")
    monkeypatch.setattr(ui_update_state, "lock_path", lambda: tmp_path / "deploy-state.lock")
    monkeypatch.setattr(
        ui_update_state,
        "lifecycle_lock_path",
        lambda: tmp_path / "deploy-state.lifecycle.lock",
    )
    monkeypatch.setattr(update, "self_holder", lambda: "gateway:pid1")
    monkeypatch.setattr(update, "update_lock_holder", lambda: "other:pid2")


def test_new_child_adopts_the_introducing_rollouts_legacy_v1_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old in-memory parent can launch the new-on-disk child mid-rollout."""
    ui_update_state.state_path().write_text(
        '{"posture":"paused","updated_at":"2026-08-24T12:34:56+00:00"}'
    )
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", lambda _holder: None)
    monkeypatch.setattr(update, "_run_gateway_orchestration_inner", lambda *_a, **_kw: 0)

    assert update._run_gateway_orchestration(Path("/unused"), origin="old-caller") == 0
    assert ui_update_state.read().status == "inactive"


def test_late_child_finally_cannot_clear_a_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", lambda _holder: None)

    def _replace_owner(*_args: object, **_kwargs: object) -> int:
        first = ui_update_state.read()
        assert first.generation is not None
        assert ui_update_state.clear(first.generation)
        ui_update_state.begin(kind="restart", origin="second")
        return 0

    monkeypatch.setattr(update, "_run_gateway_orchestration_inner", _replace_owner)

    assert update._run_gateway_orchestration(Path("/unused"), origin="first") == 0
    remaining = ui_update_state.read()
    assert remaining.status == "updating"
    assert remaining.kind == "restart"


def test_lock_loser_never_clears_the_winners_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = ui_update_state.begin(kind="rollout", origin="winner")
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: False)

    assert update._run_gateway_orchestration(Path("/unused"), origin="late-child") == 1
    remaining = ui_update_state.read()
    assert remaining.generation == winner.generation
    assert remaining.origin == "winner"


def test_child_reads_marker_only_after_it_wins_the_rollout_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquired = False

    def _acquire(*_args: object, **_kwargs: object) -> bool:
        nonlocal acquired
        acquired = True
        return True

    original_read = ui_update_state.read

    def _read_after_lock():
        assert acquired
        return original_read()

    monkeypatch.setattr(update, "acquire_update_lock", _acquire)
    monkeypatch.setattr(ui_update_state, "read", _read_after_lock)
    monkeypatch.setattr(update, "release_update_lock", lambda _holder: None)
    monkeypatch.setattr(update, "_run_gateway_orchestration_inner", lambda *_a, **_kw: 0)

    assert update._run_gateway_orchestration(Path("/unused"), origin="caller") == 0


def test_active_v2_marker_is_never_adopted_by_a_manual_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = ui_update_state.begin(kind="rollout", origin="winner")
    releases: list[str] = []
    inner_calls: list[bool] = []
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", releases.append)
    monkeypatch.setattr(
        update,
        "_run_gateway_orchestration_inner",
        lambda *_a, **_kw: inner_calls.append(True) or 0,
    )

    assert update._run_gateway_orchestration(Path("/unused"), origin="manual") == 1
    assert releases == ["gateway:pid1"]
    assert inner_calls == []
    assert ui_update_state.read().generation == winner.generation


def test_old_parent_without_marker_is_adopted_before_inner_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", lambda _holder: None)

    def _inner(*_args: object, **_kwargs: object) -> int:
        marker = ui_update_state.read()
        assert marker.status == "updating"
        assert marker.origin == "old-parent"
        return 0

    monkeypatch.setattr(update, "_run_gateway_orchestration_inner", _inner)

    assert update._run_gateway_orchestration(Path("/unused"), origin="old-parent") == 0
    assert ui_update_state.read().status == "inactive"


def test_marker_read_failure_after_lock_acquire_always_releases_db_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases: list[str] = []
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", releases.append)
    monkeypatch.setattr(
        ui_update_state, "read", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(OSError, match="disk"):
        update._run_gateway_orchestration(Path("/unused"), origin="caller")

    assert releases == ["gateway:pid1"]


def test_marker_begin_collision_releases_db_lock_without_clearing_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = ui_update_state.begin(kind="rollout", origin="winner")
    original_read = ui_update_state.read
    releases: list[str] = []
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", releases.append)
    # Force the winner child to observe the missing-state branch; begin's own
    # locked read still sees the real winner and refuses the collision.
    monkeypatch.setattr(
        ui_update_state,
        "read",
        lambda *_a, **_kw: ui_update_state.UiUpdateSnapshot(status="inactive"),
    )

    with pytest.raises(ui_update_state.UiUpdateAlreadyActive):
        update._run_gateway_orchestration(Path("/unused"), origin="loser")

    assert releases == ["gateway:pid1"]
    assert original_read().generation == winner.generation


def test_lost_phase_cas_aborts_before_stop_world_and_releases_both_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases: list[str] = []
    inner_calls: list[bool] = []
    monkeypatch.setattr(update, "acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(update, "release_update_lock", releases.append)
    monkeypatch.setattr(ui_update_state, "set_phase", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        update,
        "_run_gateway_orchestration_inner",
        lambda *_a, **_kw: inner_calls.append(True) or 0,
    )

    assert update._run_gateway_orchestration(Path("/unused"), origin="caller") == 1
    assert releases == ["gateway:pid1"]
    assert inner_calls == []
    assert ui_update_state.read().status == "inactive"
