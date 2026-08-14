"""`cmd_rollback` stop-the-world sequencing (2026-08-08 audit finding).

Rollback used to quiesce without pausing first. The restarter does not read
the paused posture — `RespawnController._dispatch_respawns` gates only on
gateway health, which stays up through the whole rollback — so it respawned
every exiting agent within its 1s poll, on the PRE-reset code, while the
schema/code transition ran; the quiesce convergence poll could never
converge. These tests pin the pause-before-quiesce order and the
unpause-on-every-exit finally.
"""

from __future__ import annotations

import pytest

from cli.commands import _cluster_rollback as _rb

_SNAP = {"00000000T000000_baseline"}


@pytest.fixture
def _seams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wire every `cmd_rollback` seam; the returned list records the sequence."""
    order: list[str] = []

    monkeypatch.setattr(_rb, "_resolve_rollback_target", lambda _to: "TARGETSHA")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_validate_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "acquire_update_lock", lambda _h, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "release_update_lock", lambda _h: order.append("release-lock"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "git_head_sha", lambda: "FROMSHA")
    monkeypatch.setattr(_rb, "current_schema_state", lambda: _SNAP)
    monkeypatch.setattr(_rb, "_quiesce_agents", lambda: order.append("quiesce"))
    monkeypatch.setattr(_rb, "_notify_agents_of_rollback", lambda _f, _t: order.append("notify"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_note_rollback_on_last_update", lambda _f, _t: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-host")

    def _pause() -> None:
        order.append("pause-local")
        from shared.host_deploy_state import set_posture

        set_posture("paused")

    def _unpause() -> None:
        order.append("unpause-local")
        # Mirror the conftest stub: the real unpause writes posture=idle, and a
        # leftover `paused` row would 503 every gateway test in this session.
        from shared.host_deploy_state import set_posture

        set_posture("idle")

    monkeypatch.setattr("ops.cluster.pause_local_cluster", _pause)
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", _unpause)
    return order


def test_rollback_pauses_local_restarter_before_quiescing(
    _seams: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pause precedes the quiesce (so no agent exiting from here on can be
    respawned mid-transition), and the unpause follows the notify (the trailing
    `ava start` has re-created the restarter by then)."""
    monkeypatch.setattr(_rb, "_run_rollback", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    rc = _rb.cmd_rollback(require_confirmation=False)

    assert rc == 0
    assert _seams.index("pause-local") < _seams.index("quiesce")
    assert _seams.index("unpause-local") > _seams.index("notify")


def test_rollback_unpauses_even_when_the_rollback_fails(
    _seams: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rollback (rc 2 = MANUAL INTERVENTION) still clears the pause in
    the finally: the recovery has been attempted, so agents must be respawnable
    again rather than stranded under a 503 posture."""
    monkeypatch.setattr(_rb, "_run_rollback", lambda *_a, **_k: 2)  # pyright: ignore[reportUnknownArgumentType]

    rc = _rb.cmd_rollback(require_confirmation=False)

    assert rc == 2
    assert _seams[0] == "pause-local"
    assert _seams[-2] == "unpause-local"
    assert _seams[-1] == "release-lock"


def test_rollback_unpause_failure_never_masks_the_outcome(
    _seams: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An unpause that itself fails (backend missing, DB down) reports and carries
    on — the rollback's own verdict is the one that matters, and the finally
    must not turn a working rollback into a reported failure."""
    monkeypatch.setattr(_rb, "_run_rollback", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def _boom() -> None:
        raise RuntimeError("backend gone")

    monkeypatch.setattr("ops.cluster.unpause_local_cluster", _boom)

    rc = _rb.cmd_rollback(require_confirmation=False)
    assert rc == 0
    assert "could not unpause" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
