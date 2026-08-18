"""Code controller — the "on-pin but running stale processes" dimension.

The regression under test is the 2026-07-28 wsl state: Phase B checked out the
target and ran `uv sync`, the restart declined, and the host was left with
`HEAD == pin` and its processes on the old commit. `_check_pin_drift` only heals
*off*-pin hosts, so nothing would ever have fixed it.

The second regression is issue #1020, at the bottom of this file: the heal deferred
to any live deploy lease, including a **settle hold naming this host** — a hold whose
entire content is "waiting for this host to converge", with nobody executing under
it. The two waited on each other and the host converged only when the TTL lapsed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.controllers import code, update_trigger
from ops.controllers.base import BlockScope
from shared.cluster_lock import DeployLease, settle_note

_PIN = "abc1234abc1234"
_OLD = "0ld0ld0ld0ld0l"
_THIS_HOST = "laptop-host"


def _lease(*, note: str | None) -> DeployLease:
    """A live lease as `read_update_lease` returns it. `note=None` is a rollout
    executing right now; a settle note is a stated waiting period with nobody
    executing under it."""
    return DeployLease(
        holder="gateway-host:pid65237", held_for_s=120.0, expires_in_s=900.0, note=note
    )


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    update_trigger.reset_cooldown()


@pytest.fixture
def stale_code_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[bool], list[int]]:
    """A host that is on-pin but running old code, with every guard open: no update
    lock, no orchestration, an isolated backoff file, and a `spawn_update` spy.
    Returns the spy's captured `restart_only` flags."""
    monkeypatch.setattr(code, "read_pin_and_head", lambda: (_PIN, _PIN))
    monkeypatch.setattr("shared.process_sha.get", lambda: _OLD)
    monkeypatch.setattr(code, "running_from_prod_source", lambda: True)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("shared.machine.machine_name", lambda: _THIS_HOST)
    monkeypatch.setattr(code, "_code_heal_attempt_path", lambda: tmp_path / "code_heal_attempt")
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    spawned: list[bool] = []

    def _spy(*, restart_only: bool = False, target_sha: str | None = None) -> dict[str, str]:
        assert target_sha is None, "the checkout is already right; the heal is a restart"
        spawned.append(restart_only)
        return {"session": "ava-updater", "log": "updater.log"}

    monkeypatch.setattr("ops.cluster.spawn_update", _spy)
    exits: list[int] = []

    def _fake_exit(code: int) -> None:
        exits.append(code)

    monkeypatch.setattr("os._exit", _fake_exit)
    return spawned, exits


def test_heals_on_pin_host_running_stale_code(
    caplog: pytest.LogCaptureFixture, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """HEAD == pin but running_sha != HEAD → spawn a restart (not a checkout) and
    block the tick. This is the roster's "code drift" state, which no controller used to own."""
    with caplog.at_level("WARNING"):
        assert code.check_code_drift() is True
    spawned, exits = stale_code_env
    assert spawned == [True]  # restart_only: the tree is already correct
    assert exits == [0]  # Task #1060: watchdog exits so the restarter respawns it aligned
    assert any("never restarted" in r.message for r in caplog.records)
    assert any("watchdog itself" in r.message for r in caplog.records)


def test_no_op_when_running_code_matches_head(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """Converged host: nothing spawned, tick not blocked, and the backoff record is
    dropped so a later drift is not silently held off by a stale one."""
    monkeypatch.setattr("shared.process_sha.get", lambda: _PIN)
    code._code_heal_attempt_path().write_text(json.dumps({"target": _PIN, "ts": 1.0, "ok": True}))
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []
    assert not code._code_heal_attempt_path().exists()


def test_defers_off_pin_host_to_the_pin_controller(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """HEAD != pin is the checkout's drift; healing the processes onto a checkout
    that is itself wrong would fight the pin controller."""
    monkeypatch.setattr(code, "read_pin_and_head", lambda: (_PIN, "d1ffd1ffd1ff"))
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_no_op_when_this_process_never_froze_a_commit(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """An unknown running_sha is not evidence of drift — never restart on a guess."""
    monkeypatch.setattr("shared.process_sha.get", lambda: None)
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_no_op_outside_the_prod_source_tree(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """A dev-worktree watchdog compares PROD's HEAD against its own tree's commit;
    that difference is the layout, not drift, and must not restart anything."""
    monkeypatch.setattr(code, "running_from_prod_source", lambda: False)
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_defers_while_a_cluster_update_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """Mid-rollout, "checkout ahead of the processes" is the normal transient — the
    rollout is what replaces them, so a heal here would fight Phase B."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _lease(note=None))
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


# ── the settle hold that names this host (issue #1020) ──────────────────────


def test_heals_under_a_settle_hold_that_names_this_host(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stale_code_env: tuple[list[bool], list[int]],
) -> None:
    """The mutual wait, broken. The hold exists BECAUSE this host has not converged
    and nothing is executing under it, so the restart it blocked is the very
    convergence it is waiting for. Before this, laptop-host sat mixed-code until the
    ~16-minute TTL lapsed — and a renewed settle window would never have released."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: _lease(note=settle_note([_THIS_HOST, "other-box"])),
    )
    with caplog.at_level("WARNING"):
        assert code.check_code_drift() is True
    spawned, _ = stale_code_env
    assert spawned == [True]
    assert any("settle hold waiting for THIS host" in r.message for r in caplog.records)


def test_defers_under_a_settle_hold_that_names_another_host(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """The permission is scoped to the named host. A hold waiting for someone else
    says nothing about this host's right to restart itself mid-window."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note(["other-box"]))
    )
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_defers_under_a_hold_whose_note_cannot_be_parsed(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """`settle_hosts` reads an unrecognised note as an empty set, and the deferral
    follows it: a note we cannot parse must never be read as permission."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note="paused for maintenance")
    )
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_a_settle_hold_naming_this_host_still_defers_to_a_local_orchestration(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """The lease permission removes ONE guard. A live `ava-updater` on this host is
    still the deploy that is going to replace these processes, and the stalled-updater
    controller ahead of this one is what clears a dead one."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note([_THIS_HOST]))
    )
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_a_settle_hold_naming_this_host_still_respects_the_backoff(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """A restart that keeps declining must not become a loop just because a hold
    names this host — the settle window is bounded but a renewed one is not."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note([_THIS_HOST]))
    )
    assert code.check_code_drift() is True
    update_trigger.reset_cooldown()
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == [True]


def test_defers_while_a_local_orchestration_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """A local self-update takes no cluster lock, so the lock check alone would let
    this fire in the middle of one."""
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_backoff_blocks_a_restart_that_did_not_land(
    stale_code_env: tuple[list[bool], list[int]],
) -> None:
    """A restart that keeps declining must not become a restart loop across the very
    restarts it spawns — the record is on disk precisely to survive them."""
    assert code.check_code_drift() is True
    update_trigger.reset_cooldown()  # clear the process cooldown; the disk backoff remains
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == [True]  # no second spawn


def test_skips_while_the_shared_cooldown_is_armed(
    stale_code_env: tuple[list[bool], list[int]],
) -> None:
    """The cooldown is shared with pin/schema so two dimensions drifting in one tick
    cannot fire two updates — a locally spawned heal must arm it too."""
    assert code.check_code_drift() is True
    assert update_trigger.in_cooldown() is True


def test_controller_is_agent_runner_only(stale_code_env: tuple[list[bool], list[int]]) -> None:
    """A gateway with stale processes needs the rollout/recovery path, not a
    self-restart."""
    controller = code.CodeController()
    result = controller.reconcile("gateway")
    assert result.acted is False
    assert result.blocks is BlockScope.NONE
    spawned, _ = stale_code_env
    assert spawned == []

    result = controller.reconcile("agent-runner")
    assert result.acted is True
    # The heal is a restart of every process on this host, so the whole roster is
    # blocked — not just the services that read the DB.
    assert result.blocks is BlockScope.ALL


# ── the spawn action's own failure paths (audit #6 gap closure) ─────────────


def test_blocks_when_the_restart_races_an_in_flight_update(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stale_code_env: tuple[list[bool], list[int]],
) -> None:
    """The orchestration guard read "none", then an updater started before the
    spawn — `spawn_update` refuses with ClusterUpdateInProgress. The heal backs
    off one tick, records nothing (the attempt never happened), and healthchecks
    run."""
    from ops.cluster import ClusterUpdateInProgress

    def _busy(*, restart_only: bool = False, target_sha: str | None = None) -> dict[str, str]:
        raise ClusterUpdateInProgress("ava-updater already exists")

    monkeypatch.setattr("ops.cluster.spawn_update", _busy)
    with caplog.at_level("WARNING"):
        assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []
    assert not code._code_heal_attempt_path().exists(), "a refused spawn is not an attempt"


def test_records_failure_when_the_restart_spawn_raises(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """A spawn that crashes for a persistent reason must arm the backoff, or it
    retries at the cooldown cadence forever — the unbounded-retry shape PR #879
    removed from the success-only record."""

    def _boom(*, restart_only: bool = False, target_sha: str | None = None) -> dict[str, str]:
        raise RuntimeError("session spawn not available")

    monkeypatch.setattr("ops.cluster.spawn_update", _boom)
    assert code.check_code_drift() is False
    rec = json.loads(code._code_heal_attempt_path().read_text())
    assert rec["ok"] is False
    assert rec["last_error"].startswith("spawn failed:")
    # The armed backoff blocks the next tick even after the cooldown clears.
    update_trigger.reset_cooldown()
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_defers_when_the_update_lock_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """Unreadable lease evidence defers (never a crash): acting on missing
    evidence is the expensive direction for a live-host restart."""

    def _boom() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _boom)
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []


def test_defers_when_the_orchestration_session_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, stale_code_env: tuple[list[bool], list[int]]
) -> None:
    """The code controller used to let this read exception propagate (a drift from
    the pin copy); the shared layer made the conservative pin semantics uniform."""

    def _boom() -> str | None:
        raise RuntimeError("session spawn not available")

    monkeypatch.setattr("ops.cluster.current_orchestration", _boom)
    assert code.check_code_drift() is False
    spawned, _ = stale_code_env
    assert spawned == []
