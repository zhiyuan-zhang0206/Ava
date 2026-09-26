"""Public PITR submission captures one selected image and reserves before effects."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from cli.release_transition.journal import Operation, exclusive, read_operation
from cli.release_transition.request import PitrRequest, ReleaseRef
from cli.release_transition.submit import submit_request
from services.pitr.activation_state import ActivationRecord, load_record, record_path
from shared.runtime_release import current_pointer, verify_release
from shared.start_inputs import configuration_files, files_digest
from shared.verified_file import regular_bytes


def selected_image(home: Path) -> ReleaseRef:
    """Capture source/schema facts only through the selected verified inventory."""
    from shared.release_identity import (
        ApplicationIdentity,
        application_identity_members,
        read_application_identity,
    )

    selected = current_pointer(home / "releases")
    if selected is None:
        raise ValueError("PITR requires a selected retained runtime image")
    artifact, manifest_digest = selected
    root = home / "releases" / artifact
    manifest = json.loads(regular_bytes(root / "manifest.json", max_bytes=32 * 1024 * 1024))
    image = verify_release(
        home / "releases",
        artifact,
        manifest_digest=manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=manifest["schema_digest"],
    )
    member = application_identity_members(manifest["files"], manifest["platform"])[0]
    candidate = ApplicationIdentity.model_validate_json(regular_bytes(root / member))
    identity = read_application_identity(image, candidate.source_commit)
    return ReleaseRef(
        artifact_digest=artifact,
        manifest_digest=manifest_digest,
        schema_digest=identity.schema_digest,
        source_commit=identity.source_commit,
    )


def _active(home: Path) -> Operation | None:
    try:
        path = Path(regular_bytes(home / "updates/active").decode().strip())
    except FileNotFoundError:
        return None
    operation = read_operation(path)
    if operation.request.home != str(home):
        raise ValueError("active operation belongs to a different home")
    return operation


def _rollback(operation: Operation) -> PitrRequest:
    from cli.release_transition import native
    from cli.release_transition.pitr_inputs import read_record

    if not isinstance(operation.request, PitrRequest):
        raise TypeError("PITR cannot take over a release operation")
    if operation.launch_attempted:
        if operation.launch is None:
            raise ValueError("PITR executor has no retained launch receipt")
        # The shared retirement adapter proves the whole domain closed first,
        # including replay after an earlier caller already removed the unit.
        native.for_launch(operation.launch).retire_current(operation.launch)
    with exclusive(operation.request.path) as journal:
        record = read_record(journal.operation)
        if record is None:
            raise RuntimeError("PITR rollback has no captured activation record")
        digest = hashlib.sha256(
            regular_bytes(record_path(Path(operation.request.home)))
        ).hexdigest()
        from shared import pause_owner

        current = pause_owner.read()
        at = journal.operation.maintenance_at
        if current.status == "paused":
            if not current.matches(str(operation.request.id), at):
                raise RuntimeError("rollback cannot adopt a foreign maintenance hold")
        elif current.status == "resumed" and current.matches(str(operation.request.id), at):
            if journal.operation.phase not in {"resuming", "proving"}:
                raise RuntimeError("PITR hold resumed outside its recorded resume boundary")
            at = datetime.now(UTC)
        elif current.status == "invalid" or journal.operation.phase not in {
            "prepared",
            "provisioning",
            "quiescing",
        }:
            raise RuntimeError("rollback cannot proceed with unknown maintenance ownership")
        journal.decide_rollback(digest, maintenance_at=at)
    return operation.request


def _join_active(active: Operation, action: Literal["activate", "rollback"]) -> PitrRequest:
    if not isinstance(active.request, PitrRequest) or active.pitr is None:
        raise RuntimeError("another home operation is incomplete")
    if action == active.pitr.action:
        return active.request
    if action == "rollback":
        return _rollback(active)
    raise RuntimeError("an explicit rollback must finish before another activation")


def prepare_request(action: Literal["activate", "rollback"], *, origin: str) -> PitrRequest:
    from shared.paths import ava_home

    home = ava_home()
    active = _active(home)
    if active is not None and not active.terminal:
        return _join_active(active, action)
    record = load_record(home)
    if record is None and action == "rollback":
        raise ValueError("PITR activation has not started")
    completed_phase = "protected" if action == "activate" else "rolled_back"
    if record is not None and record.phase == completed_phase:
        return _completed_request(home, record, action)
    return _new_request(home, action, origin, record)


def _completed_request(
    home: Path, record: ActivationRecord, action: Literal["activate", "rollback"]
) -> PitrRequest:
    """A repeated completed action joins its exact original business receipt."""
    from cli.release_transition.pitr_inputs import read_record

    if record.home_operation is None:
        raise ValueError("completed activation record has no captured home operation")
    path = home / "updates" / str(UUID(record.home_operation)) / "operation.json"
    try:
        completed = read_operation(path)
    except FileNotFoundError as exc:
        raise ValueError("completed activation record has no retained home journal") from exc
    progress, request = completed.pitr, completed.request
    if (
        not isinstance(request, PitrRequest)
        or progress is None
        or completed.phase != "complete"
        or request.home != str(home)
        or str(request.activation_id) != record.operation_id
        or (progress.action, progress.generation) != (action, record.home_generation)
        or record.home_action != action
        or read_record(completed) != record
    ):
        raise ValueError("completed activation record differs from its home operation generation")
    return request


def _new_request(
    home: Path,
    action: Literal["activate", "rollback"],
    origin: str,
    record: ActivationRecord | None,
) -> PitrRequest:
    from shared.cluster import registry_path
    from shared.machine import machine_name

    encoded = None if record is None else regular_bytes(record_path(home))
    files = configuration_files(home)
    activation_id = (
        uuid4()
        if record is None or (action == "activate" and record.phase == "rolled_back")
        else UUID(record.operation_id)
    )
    return PitrRequest(
        id=uuid4(),
        home=str(home),
        registry=str(registry_path().resolve(strict=True)),
        created_at=datetime.now(UTC),
        platform_tag=platform.platform(),
        machine=machine_name(),
        image=selected_image(home),
        activation_id=activation_id,
        action=action,
        generation=1
        if record is None or record.home_generation is None
        else record.home_generation + 1,
        expected_record=None if encoded is None else hashlib.sha256(encoded).hexdigest(),
        origin=origin,
        configuration_files=files,
        configuration_digest=files_digest(files),
    )


def run_pitr(action: Literal["activate", "rollback"], *, origin: str) -> int:
    try:
        path, native = submit_request(prepare_request(action, origin=origin))
    except (ValueError, RuntimeError, OSError) as exc:
        sys.stderr.write(f"PITR {action} refused: {exc}\n")
        return 1
    operation = read_operation(path)
    sys.stdout.write(
        json.dumps({"operation": str(path), "phase": operation.phase, "native": native}) + "\n"
    )
    return 0
