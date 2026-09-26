"""Release intent survives interruption; replay never adopts another decision."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue

from cli.release_transition import journal
from cli.release_transition.request import ReleaseRef, Request


@pytest.fixture
def request_record(tmp_path: Path) -> Request:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64})
    (home / "releases").mkdir()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": previous.artifact_digest,
                "manifest_digest": previous.manifest_digest,
            }
        )
    )
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        platform_tag="Linux-test",
        machine="test-unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


def _active(request: Request) -> Path:
    return Path(request.home) / "updates/active"


@pytest.mark.parametrize("missing", [False, True])
def test_initial_admission_reads_predecessor_after_acquiring_home_lock(
    request_record: Request, monkeypatch: pytest.MonkeyPatch, *, missing: bool
) -> None:
    pointer = Path(request_record.home) / "releases/current-release"
    original_lock = journal.file_lock

    @contextmanager
    def selector_changes_before_lock_entry(path: Path, *, timeout_s: float) -> Generator[None]:
        with original_lock(path, timeout_s=timeout_s):
            if missing:
                pointer.unlink()
            else:
                pointer.write_text(
                    json.dumps(
                        {
                            "artifact_digest": request_record.candidate.artifact_digest,
                            "manifest_digest": request_record.candidate.manifest_digest,
                        }
                    )
                )
            yield

    monkeypatch.setattr(journal, "file_lock", selector_changes_before_lock_entry)
    with pytest.raises(ValueError, match="predecessor is not the selected"):
        journal.create(request_record)
    assert not request_record.path.parent.exists()
    assert not _active(request_record).exists()


@pytest.mark.parametrize("missing_active", [False, True])
def test_replay_after_selecting_candidate_does_not_readmit_predecessor(
    request_record: Request, *, missing_active: bool
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        _advance(handle, "starting")
        retained = handle.operation
    pointer = Path(request_record.home) / "releases/current-release"
    pointer.write_text(
        json.dumps(
            {
                "artifact_digest": request_record.candidate.artifact_digest,
                "manifest_digest": request_record.candidate.manifest_digest,
            }
        )
    )
    if missing_active:
        _active(request_record).unlink()
    before = _file_state(request_record.path)
    assert journal.create(request_record) == retained
    assert _file_state(request_record.path) == before


def _file_state(path: Path) -> tuple[bytes, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mtime_ns


def _advance(handle: journal.Journal, target: journal.Phase) -> None:
    sequence: tuple[journal.Phase, ...] = (
        "quiescing",
        "stopping",
        "selecting",
        "starting",
        "observing",
        "resuming",
        "complete",
    )
    for phase in sequence:
        handle.advance(phase)
        if phase == target:
            return
    raise AssertionError(f"unknown test target: {target}")


@pytest.mark.parametrize("phase", ["stopping", "selecting", "starting", "resuming"])
def test_interrupted_intent_is_retained_and_exact_replay_is_read_only(
    request_record: Request, phase: journal.Phase
) -> None:
    journal.create(request_record)
    with pytest.raises(KeyboardInterrupt), journal.exclusive(request_record.path) as handle:
        handle.record_launch({"pid": 123, "birth": 456.0, "starttime": 789})
        _advance(handle, phase)
        raise KeyboardInterrupt

    intent = journal.read_operation(request_record.path)
    assert intent.phase == phase
    assert not intent.terminal
    before = (_file_state(request_record.path), _file_state(_active(request_record)))
    assert journal.create(request_record) == intent
    assert (_file_state(request_record.path), _file_state(_active(request_record))) == before

    with journal.exclusive(request_record.path) as handle:
        failed = handle.fail("native outcome remains unobserved")
    assert failed.phase == phase
    assert failed.direction == "candidate"
    assert failed.launch == intent.launch
    assert failed.revision == intent.revision + 1
    assert journal.create(request_record) == failed


@pytest.mark.parametrize("field", ["configuration_digest", "registry", "machine"])
def test_same_id_cannot_mutate_captured_inputs(request_record: Request, field: str) -> None:
    journal.create(request_record)
    before = (_file_state(request_record.path), _file_state(_active(request_record)))
    replacement = {
        "configuration_digest": "0" * 64,
        "registry": str(Path(request_record.home).parent / "other-registry.json"),
        "machine": "another-machine",
    }[field]
    changed = Request.model_validate(request_record.model_dump() | {field: replacement})
    with pytest.raises(ValueError, match="cannot change its inputs"):
        journal.create(changed)
    assert (_file_state(request_record.path), _file_state(_active(request_record))) == before


def test_competing_operation_refused_until_prior_is_terminal(request_record: Request) -> None:
    journal.create(request_record)
    competing = request_record.model_copy(update={"id": uuid4()})
    with journal.exclusive(request_record.path) as handle:
        _advance(handle, "observing")
        handle.fail("readiness failed")
    before = (_file_state(request_record.path), _file_state(_active(request_record)))
    with pytest.raises(ValueError, match="remains incomplete"):
        journal.create(competing)
    assert not competing.path.parent.exists()
    assert (_file_state(request_record.path), _file_state(_active(request_record))) == before

    with journal.exclusive(request_record.path) as handle:
        handle.advance("resuming")
        handle.advance("complete")
    completed = _file_state(request_record.path)
    assert journal.create(competing).phase == "prepared"
    assert _active(competing).read_text().strip() == str(competing.path)
    assert _file_state(request_record.path) == completed
    with (
        pytest.raises(ValueError, match="not the home's active"),
        journal.exclusive(request_record.path),
    ):
        pytest.fail("retired operation regained mutation authority")
    with pytest.raises(ValueError, match="remains incomplete"):
        journal.create(request_record)


def test_interrupted_active_publication_preserves_the_written_intent(
    request_record: Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = journal.write_text_atomic

    def interrupt_pointer(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == _active(request_record):
            raise KeyboardInterrupt
        original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "write_text_atomic", interrupt_pointer)
        with pytest.raises(KeyboardInterrupt):
            journal.create(request_record)
    retained = _file_state(request_record.path)
    assert not _active(request_record).exists()
    assert journal.create(request_record).phase == "prepared"
    assert _file_state(request_record.path) == retained
    assert _active(request_record).read_text().strip() == str(request_record.path)


def test_missing_active_pointer_does_not_reset_uncertain_generation(
    request_record: Request,
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        _advance(handle, "starting")
        intent = handle.fail("start dispatched; acknowledgement lost")
    _active(request_record).unlink()
    before = _file_state(request_record.path)
    assert journal.create(request_record) == intent
    assert _file_state(request_record.path) == before


def test_interrupted_native_dispatch_cannot_be_repeated(request_record: Request) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        with pytest.raises(ValueError, match="unattempted launch intent"):
            handle.mark_launch_attempted()
        with pytest.raises(ValueError, match="cannot precede native dispatch"):
            handle.record_native({"pid": 123})
        handle.record_launch({"job": "retained-external-executor"})
        dispatched = handle.mark_launch_attempted()
    assert dispatched.launch_attempted
    assert dispatched.native is None
    assert journal.create(request_record) == dispatched
    with journal.exclusive(request_record.path) as handle:
        before = _file_state(request_record.path)
        with pytest.raises(ValueError, match="unattempted launch intent"):
            handle.mark_launch_attempted()
    assert _file_state(request_record.path) == before


def test_native_birth_is_recorded_once_and_retained_across_replay(request_record: Request) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        handle.record_launch({"job": "retained-external-executor"})
        handle.mark_launch_attempted()
        birth: dict[str, JsonValue] = {"pid": 123, "birth": 456.0, "starttime": 789}
        handle.record_native(birth)
        before = _file_state(request_record.path)
        handle.record_native(birth)
        assert _file_state(request_record.path) == before
        with pytest.raises(ValueError, match="executor identity changed"):
            handle.record_native(birth | {"starttime": 999})
        expected = handle.operation
    assert journal.create(request_record) == expected
    assert _file_state(request_record.path) == before


def test_stale_journal_cas_cannot_overwrite_a_newer_decision(request_record: Request) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        stale = journal.Journal(handle.operation)
        current = handle.advance("quiescing")
        before = _file_state(request_record.path)
        with pytest.raises(ValueError, match="changed while executing"):
            stale.fail("overwrite from stale executor")
    assert journal.read_operation(request_record.path) == current
    assert _file_state(request_record.path) == before


@pytest.mark.parametrize("phase", ["starting", "observing"])
def test_recovery_direction_is_durable_and_cannot_reverse_again(
    request_record: Request, phase: journal.Phase
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        _advance(handle, phase)
        recovered = handle.recover("candidate failed")
    assert recovered.direction == "previous"
    assert recovered.phase == "stopping"
    assert journal.create(request_record) == recovered
    with journal.exclusive(request_record.path) as handle:
        phases: tuple[journal.Phase, ...] = (
            "selecting",
            "starting",
            "observing",
            "resuming",
            "complete",
        )
        for next_phase in phases:
            handle.advance(next_phase)
            before = _file_state(request_record.path)
            with pytest.raises(ValueError):
                handle.recover("reverse the recovery")
            assert _file_state(request_record.path) == before
        with pytest.raises(ValueError, match="cannot become failed"):
            handle.fail("late failure")
    assert journal.read_operation(request_record.path).direction == "previous"


@pytest.mark.parametrize("phase", ["prepared", "quiescing", "stopping", "selecting", "resuming"])
def test_recovery_requires_a_failed_candidate_start(
    request_record: Request, phase: journal.Phase
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        if phase != "prepared":
            _advance(handle, phase)
        before = _file_state(request_record.path)
        with pytest.raises(ValueError, match="only candidate startup"):
            handle.recover("not a candidate startup failure")
    assert _file_state(request_record.path) == before


def test_transition_cannot_skip_intent_or_regress(request_record: Request) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        invalid: tuple[journal.Phase, ...] = ("starting", "complete", "prepared")
        for phase in invalid:
            with pytest.raises(ValueError, match="invalid release transition"):
                handle.advance(phase)
        handle.record_launch({"pid": 123})
        retained = _file_state(request_record.path)
        handle.record_launch({"pid": 123})
        assert _file_state(request_record.path) == retained
        with pytest.raises(ValueError, match="replace its recorded native launch"):
            handle.record_launch({"pid": 456})
        handle.advance("quiescing")
        with pytest.raises(ValueError, match="invalid release transition"):
            handle.advance("prepared")


@pytest.mark.parametrize("target", ["updates", "generation", "operation", "active", "lock"])
def test_symlink_storage_is_refused_without_changing_the_target(
    request_record: Request, tmp_path: Path, target: str
) -> None:
    home = Path(request_record.home)
    outside = tmp_path / "outside"
    outside.mkdir()
    if target in {"updates", "generation"}:
        link = home / "updates" if target == "updates" else request_record.path.parent
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
    else:
        request_record.path.parent.mkdir(parents=True)
        if target == "operation":
            destination = outside / "operation.json"
            destination.write_text(journal.Operation(request=request_record).model_dump_json())
            link = request_record.path
        else:
            destination = outside / "sentinel"
            destination.write_text("retain this evidence")
            link = (
                _active(request_record) if target == "active" else home / "updates/operation.lock"
            )
        link.symlink_to(destination)
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    with pytest.raises((ValueError, RuntimeError, OSError)):
        journal.create(request_record)
    assert link.is_symlink()
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before


def test_dangling_journal_symlink_is_not_replaced_as_a_fresh_operation(
    request_record: Request, tmp_path: Path
) -> None:
    request_record.path.parent.mkdir(parents=True)
    missing = tmp_path / "missing-intent.json"
    request_record.path.symlink_to(missing)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        journal.create(request_record)
    assert request_record.path.is_symlink()
    assert not missing.exists()
    assert not _active(request_record).exists()


@pytest.mark.parametrize("target", ["operation", "active"])
def test_corrupt_active_state_is_not_reinitialized(request_record: Request, target: str) -> None:
    journal.create(request_record)
    path = request_record.path if target == "operation" else _active(request_record)
    path.write_text("{interrupted-or-corrupt")
    before = (_file_state(request_record.path), _file_state(_active(request_record)))
    with pytest.raises((ValueError, OSError)):
        journal.create(request_record)
    assert (_file_state(request_record.path), _file_state(_active(request_record))) == before


def test_active_pointer_cannot_adopt_another_home(request_record: Request, tmp_path: Path) -> None:
    foreign_home = tmp_path.resolve() / "foreign"
    foreign_home.mkdir()
    foreign = request_record.model_copy(update={"home": str(foreign_home), "id": uuid4()})
    (foreign_home / "releases").mkdir()
    (foreign_home / "releases/current-release").write_bytes(
        (Path(request_record.home) / "releases/current-release").read_bytes()
    )
    journal.create(foreign)
    journal.create(request_record)
    _active(request_record).write_text(str(foreign.path) + "\n")
    before = (_file_state(request_record.path), _file_state(foreign.path))
    with pytest.raises(ValueError, match="belongs to another home"):
        journal.create(request_record)
    assert (_file_state(request_record.path), _file_state(foreign.path)) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": 1},
        {"attempt": 1, "retired_executors": ({"attempt": 2},)},
        {"launch_attempted": True},
        {"native": {"pid": 123}},
    ],
)
def test_incoherent_executor_history_is_rejected(
    request_record: Request, changes: dict[str, Any]
) -> None:
    encoded = journal.Operation(request=request_record).model_dump() | changes
    with pytest.raises(ValueError):
        journal.Operation.model_validate(encoded)


def test_repeated_continuations_cannot_publish_an_unreadable_journal(
    request_record: Request,
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        handle.record_launch({"unit": "initial", "boot_id": "boot"})
        handle.mark_launch_attempted()
        _advance(handle, "starting")
        # Retain a realistic lower phase after a first proven-dead executor.
        # The large opaque launch field makes the real byte limit reachable
        # without hundreds of native attempts or a test-only smaller limit.
    for attempt in range(8):
        with journal.exclusive(request_record.path) as handle:
            current = handle.operation
            assert current.launch is not None
            terminal: dict[str, JsonValue] = {
                "unit": current.launch["unit"],
                "boot_id": "boot",
                "owner": None,
                "sub": "failed",
            }
            handle.request_retirement(terminal)
            handle.record_retired()
            handle.relaunch(terminal)
            before = _file_state(request_record.path)
            expected = handle.operation
            try:
                handle.record_launch(
                    {"unit": f"next-{attempt}", "boot_id": "boot", "payload": "x" * 65536}
                )
            except ValueError as exc:
                assert "capacity exceeded" in str(exc)
                assert _file_state(request_record.path) == before
                assert journal.read_operation(request_record.path) == expected
                assert expected.phase == "starting" and expected.attempt > 1
                return
            handle.mark_launch_attempted()
    pytest.fail("continuation history grew without enforcing the journal read limit")


def test_journal_capacity_counts_utf8_bytes_before_atomic_publication(
    request_record: Request,
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        before = _file_state(request_record.path)
        with pytest.raises(ValueError, match="capacity exceeded"):
            handle.record_launch({"payload": "\N{LATIN SMALL LETTER E WITH ACUTE}" * 140000})
        assert _file_state(request_record.path) == before
        assert journal.read_operation(request_record.path) == handle.operation


def test_native_retirement_intent_and_absence_survive_crashes(request_record: Request) -> None:
    journal.create(request_record)
    closed: dict[str, JsonValue] = {
        "unit": "executor-0",
        "boot_id": "boot",
        "owner": None,
        "sub": "failed",
        "invocation_id": "invocation-0",
        "cgroup": "/system.slice/executor-0",
    }
    with journal.exclusive(request_record.path) as handle:
        with pytest.raises(ValueError, match="deletion intent"):
            handle.record_retired()
        handle.record_launch({"unit": "executor-0", "boot_id": "boot"})
        handle.mark_launch_attempted()
        handle.record_native(
            {key: closed[key] for key in ("unit", "boot_id", "invocation_id", "cgroup")}
        )
        _advance(handle, "starting")
        requested = handle.request_retirement(closed)
        with pytest.raises(ValueError, match="completed native retirement"):
            handle.relaunch(closed)
    # After a controller death, missing native state is reconciled only
    # against the retained deletion intent, never treated as fresh permission.
    assert journal.read_operation(request_record.path) == requested
    with journal.exclusive(request_record.path) as handle:
        before = _file_state(request_record.path)
        assert handle.request_retirement(closed) == requested
        assert _file_state(request_record.path) == before
        with pytest.raises(ValueError, match="changed the recorded"):
            handle.request_retirement(closed | {"invocation_id": "foreign"})
        absent = handle.record_retired()
    assert journal.read_operation(request_record.path) == absent
    with journal.exclusive(request_record.path) as handle:
        before = _file_state(request_record.path)
        assert handle.record_retired() == absent
        assert _file_state(request_record.path) == before
        continued = handle.relaunch(closed)
    assert continued.phase == "starting" and continued.direction == "candidate"
    assert continued.attempt == 1 and continued.retirement is None
    assert continued.launch is None and not continued.launch_attempted
    assert continued.retired_executors[0]["retirement"] == {
        "terminal": closed,
        "state": "absent",
    }


@pytest.mark.parametrize("bad", [{"owner": {"pid": 123}}, {"sub": "running"}, {"unit": "foreign"}])
def test_native_cleanup_cannot_retire_live_or_foreign_jobs(
    request_record: Request, bad: dict[str, JsonValue]
) -> None:
    journal.create(request_record)
    with journal.exclusive(request_record.path) as handle:
        handle.record_launch({"unit": "executor-0", "boot_id": "boot"})
        handle.mark_launch_attempted()
        before = _file_state(request_record.path)
        closed: dict[str, JsonValue] = {
            "unit": "executor-0",
            "boot_id": "boot",
            "owner": None,
            "sub": "failed",
        }
        with pytest.raises(ValueError):
            handle.request_retirement(closed | bad)
        assert _file_state(request_record.path) == before


def test_new_operation_requires_previous_executor_retirement(request_record: Request) -> None:
    journal.create(request_record)
    next_request = request_record.model_copy(update={"id": uuid4()})
    with journal.exclusive(request_record.path) as handle:
        handle.record_launch({"unit": "executor-0", "boot_id": "boot"})
        handle.mark_launch_attempted()
        _advance(handle, "complete")
    before = _file_state(_active(request_record))
    with pytest.raises(ValueError, match="completed native retirement"):
        journal.create(next_request)
    assert not next_request.path.exists() and _file_state(_active(request_record)) == before
    with journal.exclusive(request_record.path) as handle:
        handle.request_retirement(
            {"unit": "executor-0", "boot_id": "boot", "owner": None, "sub": "exited"}
        )
    with pytest.raises(ValueError, match="completed native retirement"):
        journal.create(next_request)
    with journal.exclusive(request_record.path) as handle:
        handle.record_retired()
    assert journal.create(next_request).request == next_request
