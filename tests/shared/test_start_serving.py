"""The per-start serving marker gates recovery actions until readiness succeeds."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from shared.runtime_interpreter import LoadedRuntimeIdentity
from shared.start_serving import RootBirth


@pytest.fixture
def state_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    return path


def test_new_start_attempt_invalidates_a_previous_serving_generation(
    serving_root: RootBirth, state_path: Path
) -> None:
    """A stale success marker cannot admit revival during a new failed boot.

    Replacing the matching-generation check with an unconditional serving write
    would let an earlier start mark the later attempt serving and fail this test.
    """
    from shared import start_serving

    previous = start_serving.begin_start()
    assert start_serving.mark_serving(previous, runtime=serving_root.runtime) is True
    assert start_serving.is_serving() is True

    current = start_serving.begin_start()

    assert start_serving.is_serving() is False
    assert start_serving.mark_serving(previous, runtime=serving_root.runtime) is False
    assert start_serving.mark_serving(current, runtime=serving_root.runtime) is True
    assert start_serving.is_serving() is True


def test_clear_serving_keeps_revival_blocked(serving_root: RootBirth, state_path: Path) -> None:
    """Stopping a host removes its authority to revive work."""
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True

    start_serving.clear_serving()

    assert start_serving.is_serving() is False


def test_start_waits_for_an_authorized_recovery_action(
    serving_root: RootBirth, state_path: Path
) -> None:
    """A new start cannot close the gate between authorization and revival.

    Removing the lock held by ``recovery_permitted`` would let the second
    thread finish ``begin_start`` before the action completes, reproducing the
    pre-readiness check-then-act race.
    """
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    recovery_authorized = Event()
    release_recovery = Event()
    start_completed = Event()

    def recover() -> None:
        with start_serving.recovery_permitted() as permitted:
            assert permitted is True
            recovery_authorized.set()
            assert release_recovery.wait(timeout=2)

    def start_again() -> None:
        start_serving.begin_start()
        start_completed.set()

    recovery_thread = Thread(target=recover)
    recovery_thread.start()
    assert recovery_authorized.wait(timeout=2)
    start_thread = Thread(target=start_again)
    start_thread.start()

    assert start_completed.wait(timeout=0.1) is False
    release_recovery.set()
    recovery_thread.join(timeout=2)
    start_thread.join(timeout=2)
    assert start_completed.is_set()
    assert start_serving.is_serving() is False


@pytest.mark.parametrize("change", ["root", "launch", "runtime", "home", "unknown"])
def test_old_birth_cannot_grant_after_observed_generation_changes(
    serving_root: RootBirth, state_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from shared import start_serving
    from shared.root_control.client import RootClientError

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime)
    original = state_path.read_bytes()
    observed = serving_root
    if change == "root":
        process = serving_root.process.model_copy(update={"starttime": 457})
        observed = serving_root.model_copy(update={"process": process})
    elif change == "launch":
        observed = serving_root.model_copy(update={"launch_digest": "c" * 64})
    elif change == "runtime":
        runtime = serving_root.runtime.model_copy(update={"source_digest": "c" * 64})
        observed = serving_root.model_copy(update={"runtime": runtime})
    elif change == "home":
        observed = serving_root.model_copy(update={"home": "/another/home"})

    def current() -> RootBirth:
        if change == "unknown":
            raise RootClientError("native peer unreadable")
        return observed

    monkeypatch.setattr(start_serving, "_observe_root", current)
    assert start_serving.born_identity() is None
    assert not start_serving.is_serving()
    with start_serving.recovery_permitted() as permitted:
        assert not permitted
    with pytest.raises(RuntimeError, match="no matching live"):
        start_serving.require_born_runtime()
    assert state_path.read_bytes() == original


def test_marker_rejects_a_different_loaded_runtime(
    serving_root: RootBirth, state_path: Path
) -> None:
    from shared import start_serving

    generation = start_serving.begin_start()
    other = serving_root.runtime.model_copy(update={"interpreter": "/foreign/python"})
    with pytest.raises(RuntimeError, match="admitted loaded runtime"):
        start_serving.mark_serving(generation, runtime=other)
    assert not start_serving.is_serving()


def test_born_runtime_revalidates_this_callers_loaded_code(
    serving_root: RootBirth, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime)

    def original(_home: Path) -> LoadedRuntimeIdentity:
        return serving_root.runtime

    monkeypatch.setattr(start_serving, "capture_loaded_runtime", original)
    assert start_serving.require_born_runtime().birth == serving_root
    changed = serving_root.runtime.model_copy(update={"source_digest": "d" * 64})

    def different(_home: Path) -> LoadedRuntimeIdentity:
        return changed

    monkeypatch.setattr(start_serving, "capture_loaded_runtime", different)
    with pytest.raises(RuntimeError, match="no matching live"):
        start_serving.require_born_runtime()


@pytest.mark.parametrize(
    "contents", ["{}", '{"schema_version":1,"state":"serving","generation":"old"}']
)
def test_missing_or_old_markers_are_not_birth_evidence(state_path: Path, contents: str) -> None:
    from shared import start_serving

    assert start_serving.born_identity() is None
    state_path.write_text(contents)
    assert start_serving.born_identity() is None


def test_linux_birth_uses_ticks_not_recomputed_wall_clock(serving_root: RootBirth) -> None:
    shifted = serving_root.process.model_copy(update={"create_time": 999.0})
    assert serving_root.same_generation(serving_root.model_copy(update={"process": shifted}))
    missing = serving_root.process.model_copy(update={"starttime": None})
    assert not serving_root.same_generation(serving_root.model_copy(update={"process": missing}))


def test_birth_read_refuses_marker_replaced_during_native_observation(
    serving_root: RootBirth, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime)

    def raced() -> RootBirth:
        start_serving.begin_start()
        return serving_root

    monkeypatch.setattr(start_serving, "_observe_root", raced)
    assert start_serving.born_identity() is None


def test_direct_process_e2e_injects_only_its_explicit_fixture_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runpy
    import sys

    from shared import start_serving
    from tests.e2e._proc import fixture_entrypoint

    gate = tmp_path / "e2e-serving"
    monkeypatch.setattr(sys, "argv", ["fixture", str(gate), "fixture_target"])
    # Track the deliberate test-only injection so this pytest process restores
    # both product functions even though the child entry point assigns them.
    monkeypatch.setattr(start_serving, "is_serving", start_serving.is_serving)
    monkeypatch.setattr(start_serving, "recovery_permitted", start_serving.recovery_permitted)

    def target(_module: str, **_kwargs: object) -> dict[str, object]:
        assert not start_serving.is_serving()
        gate.write_text("ready\n")
        assert start_serving.is_serving()
        with start_serving.recovery_permitted() as permitted:
            assert permitted
        gate.unlink()
        assert not start_serving.is_serving()
        return {}

    monkeypatch.setattr(runpy, "run_module", target)
    fixture_entrypoint()
