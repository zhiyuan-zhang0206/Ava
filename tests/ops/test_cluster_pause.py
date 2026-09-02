"""`ops.cluster_pause` — the pause/unpause lifecycle against the posture row.

The load-bearing pairing (R1, Task #1021): `pause_local_cluster` writes
`host_deploy_state.posture = paused` (the gateway starts 503ing, and the gate
labels that 503 "updating" from the mirror), and `unpause_local_cluster` returns
it to idle. The old `cluster_paused` file / `updating.flag` pairing was retired
by the old-signal sweep (PR5). The session and DB touches are stubbed — this test
is about the posture pairing, not session management.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ops import cluster_pause
from ops.cluster_pause import unpause_local_cluster as _real_unpause_local_cluster
from shared.host_deploy_state import HostDeployState


@pytest.fixture
def posture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every `set_posture` call so the pairing is observable without a DB."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    return calls


@pytest.fixture(autouse=True)
def _stub_sessions_and_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """pause/unpause also kill/respawn the restarter session and snapshot
    agent counts; none of that is this test's subject."""

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stubbed data plane")

    # Reached through module-namespace names bound at import time:
    monkeypatch.setattr("shared.session_backend.get_backend", _StubBackend)
    # The agent-status snapshot is best-effort (fail-fast-ok by design); a
    # stubbed data plane that raises exercises exactly that tolerance.
    monkeypatch.setattr("shared.db.connect", _boom)
    # Restarter already alive -> unpause skips the respawn (no session spawn).
    # The probe lives on the session backend (the restarter is a SERVICE, and
    # S7 moved the orchestration sessions onto the same backend), so the stub
    # answers there.
    # The conftest autouse guard stubs every `unpause_local_cluster` alias (it
    # would spawn a real process); restore the real one — the respawn is
    # already neutralized by the has-session stub above, and this test's subject
    # IS the unpause's posture write.
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", _real_unpause_local_cluster)


class _StubBackend:
    def __init__(self) -> None:
        self.has_answer = True
        self.spawned: list[str] = []

    def has_session(self, _name: str) -> bool:
        return self.has_answer

    def new_session(self, name: str, _cmd: str, _cwd: object, *, env: object, **_: object) -> bool:
        self.spawned.append(name)
        return True

    def kill_session(
        self, _name: str, graceful: bool = False, expected: bool = False
    ) -> tuple[bool, str]:
        return True, "stub"


def _state(posture: str) -> HostDeployState:
    now = datetime.now(UTC)
    return HostDeployState(
        machine="win",
        posture=posture,
        updated_at=now,
        updater_lease_expires_at=None,
    )


def test_is_paused_judges_a_pre_read_state_without_another_db_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_read() -> HostDeployState | None:
        raise AssertionError("is_paused re-read host deploy state")

    monkeypatch.setattr("shared.host_deploy_state.read", _unexpected_read)

    assert cluster_pause.is_paused(_state("paused")) is True
    assert cluster_pause.is_paused(_state("idle")) is False
    assert cluster_pause.is_paused(None) is False


def test_is_paused_without_an_argument_still_reads_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    def _read() -> HostDeployState:
        nonlocal reads
        reads += 1
        return _state("paused")

    monkeypatch.setattr("shared.host_deploy_state.read", _read)

    assert cluster_pause.is_paused() is True
    assert reads == 1


def test_pause_writes_paused_posture(posture: list[str]) -> None:
    assert posture == []

    cluster_pause.pause_local_cluster()

    assert posture == ["paused"], "the pause writes the 503 posture row"


def test_unpause_writes_idle_posture(posture: list[str]) -> None:
    cluster_pause.pause_local_cluster()

    cluster_pause.unpause_local_cluster()

    assert posture == ["paused", "idle"], "the unpause must not leave the host paused"


def test_unpause_without_pause_is_a_noop(posture: list[str]) -> None:
    """The compensating resume can arrive at a host that never paused (or already
    recovered); writing idle over an idle row is a no-op, never an error."""
    cluster_pause.unpause_local_cluster()
    assert posture == ["idle"]


def test_pause_twice_then_unpause_once_clears(posture: list[str]) -> None:
    """Idempotent pause: a second pause (e.g. a repeat Phase-A delivery) must not
    leave the host paused after a single unpause."""
    cluster_pause.pause_local_cluster()
    cluster_pause.pause_local_cluster()
    cluster_pause.unpause_local_cluster()
    assert posture == ["paused", "paused", "idle"]


def test_unpause_leaves_durably_disabled_restarter_down(
    posture: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _StubBackend()
    backend.has_answer = False
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr("shared.disabled_services.read_skipped", lambda: {"restarter"})
    # Process mode: the roster gate is None here, so the durable marker is the
    # branch under test (hosted mode would already exit at the roster gate).
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "process")

    cluster_pause.unpause_local_cluster()

    assert posture == ["idle"]
    assert backend.spawned == []


def test_unpause_fails_closed_when_disabled_marker_is_unreadable(
    posture: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _StubBackend()
    backend.has_answer = False
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)

    def _unreadable() -> set[str]:
        raise OSError("marker unavailable")

    monkeypatch.setattr("shared.disabled_services.read_skipped", _unreadable)
    # Process mode so the failure under test is the marker read, not the
    # roster gate (which in hosted mode exits before the marker is read).
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "process")

    cluster_pause.unpause_local_cluster()

    assert posture == ["idle"]
    assert backend.spawned == []


def test_unpause_respects_roster_gate_in_hosted_mode(
    posture: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted mode gates the restarter OUT of the start roster; the unpause
    finally must not resurrect it. 2026-09-02 incidents (18:56 / 22:20 / 22:28):
    every rollout's pause/unpause boundary relaunched the restarter on the new
    SHA while the ``ava start`` child had correctly skipped it — this is the
    respawn path that undid the skip."""
    backend = _StubBackend()
    backend.has_answer = False
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "hosted")

    cluster_pause.unpause_local_cluster()

    assert posture == ["idle"]
    assert backend.spawned == []


def test_unpause_respawns_restarter_when_the_roster_enables_it(
    posture: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process mode (the legacy shape) keeps the unpause respawn: the roster
    gate is None and the durable marker is clear, so the session comes back."""
    backend = _StubBackend()
    backend.has_answer = False
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "process")
    monkeypatch.setattr("shared.disabled_services.read_skipped", set)

    cluster_pause.unpause_local_cluster()

    assert posture == ["idle"]
    assert backend.spawned == ["ava-restarter"]
