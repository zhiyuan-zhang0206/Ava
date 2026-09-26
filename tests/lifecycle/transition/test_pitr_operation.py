"""PITR authority survives record/config writes without becoming a release swap."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from cli.release_transition import journal
from cli.release_transition.pitr_evidence import PitrSeal
from cli.release_transition.pitr_inputs import read_record, require_inputs
from cli.release_transition.request import PitrRequest, ReleaseRef, read_request
from services.pitr.activation_state import ActivationRecord, record_path, write_record
from shared.process_evidence import ExpectedProcess
from shared.release_operation import (
    authorized_pitr,
    authorized_start,
    require_configuration_write_authorized,
    require_pitr_authorized,
    require_start_authorized,
)
from shared.start_inputs import configuration_files, files_digest
from tests.lifecycle.transition.test_launcher_linux import _constant


@pytest.fixture
def pitr_request(tmp_path: Path) -> PitrRequest:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_SERVICE_PATH=/usr/bin\n")
    (home / "pg").mkdir()
    (home / "pg/postgresql.auto.conf").write_text("# owned\n")
    image = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    (home / "releases").mkdir()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": image.artifact_digest,
                "manifest_digest": image.manifest_digest,
            }
        )
    )
    files = configuration_files(home)
    return PitrRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=datetime.now(UTC),
        platform_tag="test",
        machine="test",
        image=image,
        activation_id=uuid4(),
        action="activate",
        generation=1,
        expected_record=None,
        origin="test",
        configuration_digest=files_digest(files),
        configuration_files=files,
    )


def _provisioning(request: PitrRequest) -> journal.Operation:
    journal.create(request)
    with journal.exclusive(request.path) as handle:
        return handle.advance("provisioning")


def _record(request: PitrRequest) -> ActivationRecord:
    return replace(
        ActivationRecord.start(operation_id=str(request.activation_id), origin="test"),
        home_operation=str(request.id),
        home_action="activate",
        home_generation=1,
    )


def _seal(request: PitrRequest, record_digest: str) -> PitrSeal:
    home = Path(request.home)
    return PitrSeal(
        configuration_digest=files_digest(configuration_files(home)),
        auto_conf_digest=hashlib.sha256(
            (home / "pg/postgresql.auto.conf").read_bytes()
        ).hexdigest(),
        archive_settings_digest="e" * 64,
        postgres=ExpectedProcess(pid=41, create_time=2.0, starttime=3),
        postgres_state={"system_identifier": "cluster"},
        activation_digest=record_digest,
    )


def test_pitr_round_trip_has_one_image_and_no_release_direction(pitr_request: PitrRequest) -> None:
    assert read_request(pitr_request.model_dump_json().encode()) == pitr_request
    operation = journal.create(pitr_request)
    assert operation.direction is None and operation.reference == pitr_request.image
    assert operation.request.executor == pitr_request.image
    with pytest.raises(ValidationError, match="PITR requires"):
        journal.Operation(request=pitr_request)
    with journal.exclusive(pitr_request.path) as handle:
        with pytest.raises(ValueError, match="automatic"):
            handle.recover("failure")
        with pytest.raises(ValueError, match="invalid release transition"):
            handle.advance("selecting")


def test_no_request_kind_fallback(pitr_request: PitrRequest) -> None:
    raw = pitr_request.model_dump(mode="json")
    del raw["kind"]
    with pytest.raises(ValidationError):
        read_request(json.dumps(raw).encode())


def test_reservation_rechecks_record_before_any_journal_or_config_effect(
    pitr_request: PitrRequest,
) -> None:
    home = Path(pitr_request.home)
    before = (home / ".env").read_bytes()
    write_record(home, _record(pitr_request))
    with pytest.raises(ValueError, match="before home reservation"):
        journal.create(pitr_request)
    assert not pitr_request.path.exists()
    assert (home / ".env").read_bytes() == before


def test_incomplete_pitr_blocks_another_home_request(pitr_request: PitrRequest) -> None:
    journal.create(pitr_request)
    second = pitr_request.model_copy(update={"id": uuid4()})
    with pytest.raises(ValueError, match="incomplete"):
        journal.create(second)
    assert not second.path.exists()


def test_active_pitr_requires_local_capability_before_business_write(
    pitr_request: PitrRequest,
) -> None:
    _provisioning(pitr_request)
    home = Path(pitr_request.home)
    with pytest.raises(RuntimeError, match="executor capability"):
        write_record(home, _record(pitr_request))
    assert not record_path(home).exists()
    with (
        journal.exclusive(pitr_request.path) as handle,
        authorized_pitr(pitr_request.path, handle.pitr_record_write),
    ):
        record = _record(pitr_request)
        write_record(home, record)
        assert read_record(handle.operation) == record
        assert handle.operation.pitr is not None
        assert handle.operation.pitr.record_intent == (
            None,
            hashlib.sha256(record_path(home).read_bytes()).hexdigest(),
        )


def test_record_write_intent_recovers_after_effect_without_accepting_unknown_bytes(
    pitr_request: PitrRequest,
) -> None:
    _provisioning(pitr_request)
    home = Path(pitr_request.home)
    with (
        journal.exclusive(pitr_request.path) as handle,
        authorized_pitr(pitr_request.path, handle.pitr_record_write),
    ):
        write_record(home, _record(pitr_request))
    # The journal still has pending intent, as after a death immediately after rename.
    recovered = journal.read_operation(pitr_request.path)
    assert read_record(recovered) is not None
    payload = json.loads(record_path(home).read_bytes())
    payload["origin"] = "unowned edit"
    record_path(home).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="captured PITR write generation"):
        read_record(recovered)


def test_capability_cannot_follow_another_action_generation(pitr_request: PitrRequest) -> None:
    _provisioning(pitr_request)
    with (
        journal.exclusive(pitr_request.path) as handle,
        authorized_pitr(pitr_request.path, handle.pitr_record_write),
    ):
        require_pitr_authorized(Path(pitr_request.home))
        raw = json.loads(pitr_request.path.read_bytes())
        raw["pitr"]["generation"] += 1
        pitr_request.path.write_text(json.dumps(raw))
        with pytest.raises(RuntimeError, match="generation"):
            require_pitr_authorized(Path(pitr_request.home))


def test_pitr_config_capability_does_not_escape_its_home(
    pitr_request: PitrRequest, tmp_path: Path
) -> None:
    _provisioning(pitr_request)
    home = Path(pitr_request.home)
    with pytest.raises(RuntimeError, match="executor"):
        require_configuration_write_authorized(home)
    with (
        journal.exclusive(pitr_request.path) as handle,
        authorized_pitr(pitr_request.path, handle.pitr_record_write),
    ):
        require_configuration_write_authorized(home)
        with pytest.raises(RuntimeError, match="executor"):
            require_pitr_authorized(tmp_path / "other")


@pytest.mark.parametrize("field", [".env", "service-selection.json", "pg/postgresql.auto.conf"])
def test_sealed_start_rejects_config_drift_before_effects(
    pitr_request: PitrRequest, field: str
) -> None:
    _provisioning(pitr_request)
    home = Path(pitr_request.home)
    with (
        journal.exclusive(pitr_request.path) as handle,
        authorized_pitr(pitr_request.path, handle.pitr_record_write),
    ):
        write_record(home, _record(pitr_request))
        seal = _seal(pitr_request, hashlib.sha256(record_path(home).read_bytes()).hexdigest())
        handle.provisioned(seal)
        require_inputs(handle.operation)
        (home / field).write_text("changed")
        with pytest.raises(ValueError):
            require_inputs(handle.operation)


def test_start_requires_seal_and_exact_journal_revision(pitr_request: PitrRequest) -> None:
    _provisioning(pitr_request)
    raw = json.loads(pitr_request.path.read_bytes())
    raw["phase"] = "starting"
    pitr_request.path.write_text(json.dumps(raw))
    with authorized_start(pitr_request.path), pytest.raises(ValueError, match="seal"):
        require_start_authorized(Path(pitr_request.home))
    raw["pitr"]["seal"] = _seal(pitr_request, "f" * 64).model_dump(mode="json")
    maintenance_at = pitr_request.created_at + timedelta(hours=1)
    raw["pitr"]["maintenance_at"] = maintenance_at.isoformat()
    pitr_request.path.write_text(json.dumps(raw))
    with authorized_start(pitr_request.path):
        assert require_start_authorized(Path(pitr_request.home)) == (
            str(pitr_request.id),
            maintenance_at,
        )
        raw["revision"] += 1
        pitr_request.path.write_text(json.dumps(raw))
        with pytest.raises(RuntimeError, match="holds startup"):
            require_start_authorized(Path(pitr_request.home))


@pytest.mark.parametrize("held", [False, True])
def test_explicit_rollback_captures_maintenance_generation(
    pitr_request: PitrRequest, held: bool
) -> None:
    _provisioning(pitr_request)
    with journal.exclusive(pitr_request.path) as handle:
        at = pitr_request.created_at if held else pitr_request.created_at + timedelta(seconds=1)
        updated = handle.decide_rollback("a" * 64, maintenance_at=at)
        assert updated.pitr is not None and updated.pitr.action == "rollback"
        assert updated.pitr.generation == 2 and updated.maintenance_at == at
        assert updated.request == pitr_request and updated.phase == "prepared"
        assert updated.pitr.decisions[0]["generation"] == 1


def test_rollback_refuses_unclosed_executor(pitr_request: PitrRequest) -> None:
    journal.create(pitr_request)
    with journal.exclusive(pitr_request.path) as handle:
        handle.record_launch({"unit": "private"})
        handle.mark_launch_attempted()
        with pytest.raises(ValueError, match="retirement"):
            handle.decide_rollback("a" * 64, maintenance_at=pitr_request.created_at)
        assert handle.operation.pitr is not None and handle.operation.pitr.action == "activate"


@pytest.mark.parametrize("post_resume", [False, True])
def test_rollback_submission_preserves_held_generation_or_renews_after_resume(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch, post_resume: bool
) -> None:
    from cli.release_transition import launcher_linux, pitr_submit
    from shared import pause_owner

    journal.create(pitr_request)
    home = Path(pitr_request.home)
    with journal.exclusive(pitr_request.path) as handle:
        handle.record_launch({"unit": "captured", "boot_id": "boot"})
        handle.mark_launch_attempted()
        handle.advance("provisioning")
        with authorized_pitr(pitr_request.path, handle.pitr_record_write):
            write_record(home, _record(pitr_request))
        handle.provisioned(
            _seal(pitr_request, hashlib.sha256(record_path(home).read_bytes()).hexdigest())
        )
        for phase in ("stopping_apps", "stopping_data", "starting"):
            handle.advance(phase)
        if post_resume:
            for phase in ("observing", "resuming", "proving"):
                handle.advance(phase)
    observed: list[str] = []

    def retire(_launch: object) -> None:
        observed.append("native closure before decision")
        with journal.exclusive(pitr_request.path) as handle:
            handle.request_retirement(
                {"owner": None, "sub": "failed", "unit": "captured", "boot_id": "boot"}
            )
            handle.record_retired()

    monkeypatch.setattr(launcher_linux, "retire_current", retire)
    monkeypatch.setattr(
        pause_owner,
        "read",
        lambda: pause_owner.PauseOwnerSnapshot(
            status="resumed" if post_resume else "paused",
            holder=str(pitr_request.id),
            acquired_at=pitr_request.created_at,
        ),
    )
    pitr_submit._rollback(journal.read_operation(pitr_request.path))
    result = journal.read_operation(pitr_request.path)
    assert observed == ["native closure before decision"]
    assert result.pitr is not None and result.pitr.record_intent is None
    assert result.pitr.action == "rollback" and result.pitr.generation == 2
    assert (
        result.maintenance_at > pitr_request.created_at
        if post_resume
        else result.maintenance_at == pitr_request.created_at
    )
    assert result.phase == "provisioning" and result.direction is None


def test_rollback_cannot_decide_while_native_attempt_is_unknown(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.release_transition import launcher_linux, pitr_submit

    journal.create(pitr_request)
    with journal.exclusive(pitr_request.path) as handle:
        handle.record_launch({"unit": "captured"})
        handle.mark_launch_attempted()
    before = pitr_request.path.read_bytes()

    def unknown(_record: object) -> None:
        raise RuntimeError("native custody unknown")

    monkeypatch.setattr(launcher_linux, "retire_current", unknown)
    with pytest.raises(RuntimeError, match="native custody unknown"):
        pitr_submit._rollback(journal.read_operation(pitr_request.path))
    assert pitr_request.path.read_bytes() == before


def test_new_rollback_after_release_uses_selected_image_and_same_activation(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.release_transition import pitr_submit
    from cli.release_transition.request import Request
    from shared import cluster, machine, paths

    # The activation was born under A. A subsequent completed release selected B.
    # The capture boundary reads the business identity; full phase proofs are
    # exercised by the activation-state controls, not fabricated here.
    home = Path(pitr_request.home)
    record = _record(pitr_request)
    write_record(home, record)
    image_b = pitr_request.image.model_copy(update={"artifact_digest": "e" * 64})
    release = Request(
        **pitr_request.model_dump(
            exclude={
                "kind",
                "image",
                "activation_id",
                "action",
                "generation",
                "expected_record",
                "origin",
                "configuration_files",
            }
        ),
        previous=pitr_request.image,
        candidate=image_b,
        executor=image_b,
    )
    completed = journal.Operation(request=release, phase="complete")
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": image_b.artifact_digest,
                "manifest_digest": image_b.manifest_digest,
            }
        )
    )
    registry = Path(pitr_request.registry)
    registry.write_text("{}")
    monkeypatch.setattr(pitr_submit, "_active", _constant(completed))
    monkeypatch.setattr(pitr_submit, "selected_image", _constant(image_b))
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(cluster, "registry_path", lambda: registry)
    monkeypatch.setattr(machine, "machine_name", lambda: pitr_request.machine)
    result = pitr_submit.prepare_request("rollback", origin="operator")
    assert result.image == image_b and result.executor == image_b
    assert result.activation_id == pitr_request.activation_id
    assert result.expected_record == hashlib.sha256(record_path(home).read_bytes()).hexdigest()
    assert result.generation == 2 and result.id != pitr_request.id


def test_pitr_refuses_selector_changes_without_touching_captured_files(
    pitr_request: PitrRequest,
) -> None:
    operation = _provisioning(pitr_request)
    home = Path(pitr_request.home)
    before = (home / ".env").read_bytes()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": "f" * 64,
                "manifest_digest": pitr_request.image.manifest_digest,
            }
        )
    )
    with pytest.raises(ValueError, match="selected image changed"):
        require_inputs(operation)
    assert (home / ".env").read_bytes() == before


def test_pitr_input_admission_imports_without_settings_or_database() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    program = (
        f"import sys; sys.path.insert(0, {str(root)!r}); "
        "import cli.release_transition.pitr_inputs; "
        "assert not {'shared.config', 'shared.db', 'shared.runtime_config'} & sys.modules.keys()"
    )
    result = subprocess.run(  # noqa: S603 — fixed import probe in the candidate source
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "changed", [None, "pid", "starttime", "missing_tick", "boot_id", "invocation_id", "cgroup"]
)
def test_native_exec_replay_preserves_original_receipt_and_strict_native_identity(
    pitr_request: PitrRequest, changed: str | None
) -> None:
    journal.create(pitr_request)
    original: dict[str, JsonValue] = {
        "pid": 41,
        "birth": 100.0,
        "starttime": 80,
        "unit": "private",
        "boot_id": "boot",
        "invocation_id": "invocation",
        "cgroup": "/private",
    }
    replay = original | {"birth": 101.0}
    if changed == "missing_tick":
        replay["starttime"] = None
    elif changed is not None:
        replay[changed] = 42 if changed in {"pid", "starttime"} else "replacement"
    with journal.exclusive(pitr_request.path) as handle:
        handle.record_launch({"unit": "private"})
        handle.mark_launch_attempted()
        handle.record_native(original)
        before = pitr_request.path.read_bytes()
        if changed is None:
            handle.record_native(replay)
        else:
            with pytest.raises(ValueError, match="identity changed"):
                handle.record_native(replay)
        assert handle.operation.native == original
        assert pitr_request.path.read_bytes() == before


def test_no_tick_native_exec_replay_rejects_even_small_birth_drift(
    pitr_request: PitrRequest,
) -> None:
    journal.create(pitr_request)
    original: dict[str, JsonValue] = {"pid": 41, "birth": 100.0, "starttime": None}
    with journal.exclusive(pitr_request.path) as handle:
        handle.record_launch({"unit": "private"})
        handle.mark_launch_attempted()
        handle.record_native(original)
        with pytest.raises(ValueError, match="identity changed"):
            handle.record_native(original | {"birth": 100.001})
        assert handle.operation.native == original
