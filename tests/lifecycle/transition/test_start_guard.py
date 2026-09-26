"""Ordinary startup cannot consume an interrupted release executor's authority."""

from __future__ import annotations

import json
from contextvars import Context  # noqa: TID251 — verify the explicit startup capability's isolation
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cli.release_transition.journal import create
from cli.release_transition.request import ReleaseRef, Request
from shared.release_operation import authorized_start, require_start_authorized
from shared.runtime_release import ReleaseRejectedError
from shared.start_inputs import configuration_digest


def _operation(home: Path) -> Path:
    home.mkdir()
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64})
    request = Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        platform_tag="Linux-test",
        machine="test-unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest=configuration_digest(home),
    )
    (home / "releases").mkdir(exist_ok=True)
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": request.previous.artifact_digest,
                "manifest_digest": request.previous.manifest_digest,
            }
        )
    )
    create(request)
    return request.path


@pytest.fixture
def operation_path(tmp_path: Path) -> Path:
    return _operation(tmp_path.resolve() / "home")


def _home(path: Path) -> Path:
    return path.parent.parent.parent


def _set_state(path: Path, **changes: object) -> None:
    payload = json.loads(path.read_bytes())
    payload.update(changes)
    path.write_text(json.dumps(payload) + "\n")


def _state(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def test_absent_active_allows_start_without_creating_lifecycle_state(tmp_path: Path) -> None:
    home = tmp_path.resolve() / "ordinary-home"
    home.mkdir()
    require_start_authorized(home)
    assert list(home.iterdir()) == []
    updates = home / "updates"
    updates.mkdir()
    require_start_authorized(home)
    assert list(updates.iterdir()) == []


def test_active_with_missing_journal_refuses_instead_of_becoming_an_ordinary_start(
    operation_path: Path,
) -> None:
    pointer = operation_path.parent.parent / "active"
    before = _state(pointer)
    operation_path.unlink()
    with pytest.raises(FileNotFoundError):
        require_start_authorized(_home(operation_path))
    assert _state(pointer) == before
    assert not operation_path.exists()


@pytest.mark.parametrize("target_exists", [False, True])
def test_symlink_active_pointer_refuses_without_following_or_removing_it(
    operation_path: Path, target_exists: bool
) -> None:
    pointer = operation_path.parent.parent / "active"
    outside = _home(operation_path).parent / "foreign-pointer"
    if target_exists:
        outside.write_bytes(pointer.read_bytes())
    pointer.unlink()
    pointer.symlink_to(outside)
    with pytest.raises(ReleaseRejectedError):
        require_start_authorized(_home(operation_path))
    assert pointer.is_symlink()
    assert outside.exists() == target_exists


@pytest.mark.parametrize("target_exists", [False, True])
def test_symlink_operation_is_never_accepted_as_a_completed_journal(
    operation_path: Path, target_exists: bool
) -> None:
    _set_state(operation_path, phase="complete")
    outside = _home(operation_path).parent / "foreign-operation.json"
    if target_exists:
        operation_path.rename(outside)
    else:
        operation_path.unlink()
    operation_path.symlink_to(outside)
    with pytest.raises((ValueError, FileNotFoundError)):
        require_start_authorized(_home(operation_path))
    assert operation_path.is_symlink()
    assert outside.exists() == target_exists


@pytest.mark.parametrize("contents", [b"", b"relative/operation.json", b"\xff", b"null"])
def test_corrupt_pointer_refuses_without_reinitializing(
    operation_path: Path, contents: bytes
) -> None:
    pointer = operation_path.parent.parent / "active"
    pointer.write_bytes(contents)
    before = (_state(pointer), _state(operation_path))
    with pytest.raises(ValueError):
        require_start_authorized(_home(operation_path))
    assert (_state(pointer), _state(operation_path)) == before


@pytest.mark.parametrize("contents", [b"{", b"null", b"[]", b"{}"])
def test_corrupt_journal_refuses_without_reinitializing(
    operation_path: Path, contents: bytes
) -> None:
    operation_path.write_bytes(contents)
    before = _state(operation_path)
    with pytest.raises((ValueError, TypeError, KeyError)):
        require_start_authorized(_home(operation_path))
    assert _state(operation_path) == before


@pytest.mark.parametrize("field", ["home", "id", "version", "direction"])
def test_completed_journal_must_still_match_its_home_generation_and_format(
    operation_path: Path, field: str
) -> None:
    payload = json.loads(operation_path.read_bytes())
    payload["phase"] = "complete"
    if field == "direction":
        payload[field] = "unknown"
    else:
        payload["request"][field] = {
            "home": str(_home(operation_path).parent / "other-home"),
            "id": str(uuid4()),
            "version": 2,
        }[field]
    operation_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        require_start_authorized(_home(operation_path))


@pytest.mark.parametrize(
    "phase", ["prepared", "quiescing", "stopping", "selecting", "starting", "observing", "resuming"]
)
@pytest.mark.parametrize("direction", ["candidate", "previous"])
def test_interrupted_operation_holds_operator_start_and_fresh_boot_context(
    operation_path: Path, phase: str, direction: str
) -> None:
    _set_state(operation_path, phase=phase, direction=direction, error="interrupted")
    before = _state(operation_path)
    with pytest.raises(RuntimeError, match=f"holds startup at {phase}"):
        require_start_authorized(_home(operation_path))
    with pytest.raises(RuntimeError, match=f"holds startup at {phase}"):
        Context().run(require_start_authorized, _home(operation_path))
    assert _state(operation_path) == before


@pytest.mark.parametrize("direction", ["candidate", "previous"])
def test_exact_start_capability_is_scoped_and_not_inherited_by_fresh_context(
    operation_path: Path, direction: str
) -> None:
    _set_state(operation_path, phase="starting", direction=direction)
    before = _state(operation_path)
    with authorized_start(operation_path):
        payload = json.loads(operation_path.read_bytes())
        assert require_start_authorized(_home(operation_path)) == (
            payload["request"]["id"],
            datetime.fromisoformat(payload["request"]["created_at"]),
        )
        with pytest.raises(RuntimeError):
            Context().run(require_start_authorized, _home(operation_path))
    with pytest.raises(RuntimeError):
        require_start_authorized(_home(operation_path))
    assert _state(operation_path) == before


@pytest.mark.parametrize(
    "phase", ["prepared", "stopping", "selecting", "observing", "resuming", "unknown"]
)
def test_capability_cannot_authorize_a_non_starting_phase(operation_path: Path, phase: str) -> None:
    _set_state(operation_path, phase=phase)
    with authorized_start(operation_path), pytest.raises(RuntimeError):
        require_start_authorized(_home(operation_path))


@pytest.mark.parametrize("change", ["revision", "error", "whitespace"])
def test_any_changed_journal_bytes_revoke_an_existing_start_capability(
    operation_path: Path, change: str
) -> None:
    _set_state(operation_path, phase="starting")
    with authorized_start(operation_path):
        require_start_authorized(_home(operation_path))
        if change == "whitespace":
            operation_path.write_bytes(operation_path.read_bytes() + b"\n")
        elif change == "revision":
            _set_state(operation_path, revision=42)
        else:
            _set_state(operation_path, error="native start result uncertain")
        with pytest.raises(RuntimeError):
            require_start_authorized(_home(operation_path))


@pytest.mark.parametrize("member", [".env", "service-selection.json"])
def test_exact_start_capability_cannot_authorize_changed_configuration(
    operation_path: Path, member: str
) -> None:
    _set_state(operation_path, phase="starting")
    with authorized_start(operation_path):
        require_start_authorized(_home(operation_path))
        (_home(operation_path) / member).write_text("changed after preparation\n")
        with pytest.raises(ReleaseRejectedError, match="configuration changed"):
            require_start_authorized(_home(operation_path))


def test_capability_does_not_follow_an_active_pointer_to_another_generation(
    operation_path: Path,
) -> None:
    _set_state(operation_path, phase="starting")
    pointer = operation_path.parent.parent / "active"
    payload = json.loads(operation_path.read_bytes())
    payload["request"]["id"] = str(uuid4())
    replacement = operation_path.parent.parent / payload["request"]["id"] / "operation.json"
    replacement.parent.mkdir()
    replacement.write_text(json.dumps(payload))
    with authorized_start(operation_path):
        pointer.write_text(str(replacement) + "\n")
        with pytest.raises(RuntimeError):
            require_start_authorized(_home(operation_path))
    with authorized_start(replacement):
        require_start_authorized(_home(operation_path))


def test_nested_authorization_restores_outer_capability_even_after_exception(
    operation_path: Path,
) -> None:
    other = _operation(_home(operation_path).parent / "other-home")
    _set_state(operation_path, phase="starting")
    _set_state(other, phase="starting")
    with authorized_start(operation_path):
        with pytest.raises(KeyboardInterrupt), authorized_start(other):
            require_start_authorized(_home(other))
            with pytest.raises(RuntimeError):
                require_start_authorized(_home(operation_path))
            raise KeyboardInterrupt
        require_start_authorized(_home(operation_path))
        with pytest.raises(RuntimeError):
            require_start_authorized(_home(other))
    with pytest.raises(RuntimeError):
        require_start_authorized(_home(operation_path))


def test_completed_cleanup_allows_start_without_rewriting_or_removing_receipts(
    operation_path: Path,
) -> None:
    _set_state(operation_path, phase="complete", error=None)
    pointer = operation_path.parent.parent / "active"
    before = (_state(pointer), _state(operation_path))
    require_start_authorized(_home(operation_path))
    assert (_state(pointer), _state(operation_path)) == before
    pointer.unlink()
    require_start_authorized(_home(operation_path))
    assert _state(operation_path) == before[1]


def test_complete_phase_with_unresolved_error_does_not_release_startup(
    operation_path: Path,
) -> None:
    _set_state(operation_path, phase="complete", error="cleanup remains uncertain")
    with authorized_start(operation_path), pytest.raises(RuntimeError):
        require_start_authorized(_home(operation_path))


@pytest.mark.parametrize("result", [0, 4])
def test_operation_start_preserves_exact_hold_after_service_result(
    operation_path: Path, monkeypatch: pytest.MonkeyPatch, result: int
) -> None:
    from cli.commands._pause_resume import resume_after_start
    from shared import maintenance, start_serving

    _set_state(operation_path, phase="starting")
    _hold(operation_path, monkeypatch)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    calls: list[str] = []
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", lambda: calls.append("resume"))

    @resume_after_start
    def start() -> int:
        assert maintenance.start_authorized()
        calls.append("start")
        return result

    with authorized_start(operation_path):
        assert start() == result
    assert calls == ["start"]
    assert maintenance.held() and not maintenance.start_authorized()


def _hold(path: Path, monkeypatch: pytest.MonkeyPatch, *, kind: str = "exact") -> None:
    from shared import paths
    from shared.maintenance_state import MaintenanceHold

    home = _home(path)
    (home / "run").mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(paths, "run_dir", lambda: home / "run")
    if kind == "missing":
        return
    request = json.loads(path.read_bytes())["request"]
    payload = {
        "state": "paused",
        "holder": "other" if kind == "holder" else request["id"],
        "acquired_at": "2000-01-01T00:00:00+00:00"
        if kind == "timestamp"
        else request["created_at"],
        "maintenance": MaintenanceHold(
            phase="starting", failures={1: "unclosed child"} if kind == "unsettled" else {}
        ).encode(),
    }
    (home / "run/deploy-pause-owner.json").write_text(json.dumps(payload))


@pytest.mark.parametrize("kind", ["missing", "holder", "timestamp", "unsettled"])
@pytest.mark.parametrize("ambient_authority", [False, True])
def test_operation_start_cannot_bypass_missing_changed_or_unsettled_hold(
    operation_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, ambient_authority: bool
) -> None:
    from cli.commands._pause_resume import resume_after_start
    from shared import maintenance

    _set_state(operation_path, phase="starting")
    _hold(operation_path, monkeypatch, kind=kind)
    monkeypatch.setattr(maintenance, "start_authorized", lambda: ambient_authority)
    calls: list[str] = []
    start = resume_after_start(lambda: calls.append("started") or 0)
    with authorized_start(operation_path), pytest.raises(RuntimeError):
        start()
    assert calls == []


def test_operation_start_rejects_naive_maintenance_timestamp(operation_path: Path) -> None:
    payload = json.loads(operation_path.read_bytes())
    payload["phase"] = "starting"
    payload["request"]["created_at"] = "2026-01-01T00:00:00"
    operation_path.write_text(json.dumps(payload))
    with authorized_start(operation_path), pytest.raises(ValueError, match="aware"):
        require_start_authorized(_home(operation_path))
