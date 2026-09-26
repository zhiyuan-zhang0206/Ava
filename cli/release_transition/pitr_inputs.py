"""Settings-free admission of PITR's captured files and journaled record writes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from cli.release_transition.journal import Operation
from cli.release_transition.request import PitrRequest
from services.pitr.activation_state import ActivationRecord, record_path
from shared.runtime_release import current_pointer
from shared.start_inputs import configuration_files
from shared.verified_file import regular_bytes


def read_record(operation: Operation) -> ActivationRecord | None:
    progress = operation.pitr
    if progress is None:
        raise ValueError("PITR record admission requires PITR progress")
    try:
        encoded = regular_bytes(record_path(Path(operation.request.home)))
    except FileNotFoundError:
        encoded = None
    digest = None if encoded is None else hashlib.sha256(encoded).hexdigest()
    accepted = {progress.record_digest}
    if progress.record_intent is not None:
        accepted.add(progress.record_intent[1])
    if digest not in accepted:
        raise ValueError("activation record differs from the captured PITR write generation")
    return None if encoded is None else ActivationRecord.from_json(encoded.decode())


def require_inputs(operation: Operation) -> None:
    request, progress = operation.request, operation.pitr
    if not isinstance(request, PitrRequest) or progress is None:
        raise ValueError("PITR input admission requires its typed request")
    home = Path(request.home)
    if current_pointer(home / "releases") != request.image.selector:
        raise ValueError("PITR selected image changed outside its captured operation")
    record = read_record(operation)
    observed = configuration_files(home)
    if progress.seal is not None:
        operation.require_configuration()
        if (
            hashlib.sha256(regular_bytes(home / "pg/postgresql.auto.conf")).hexdigest()
            != progress.seal.auto_conf_digest
        ):
            raise ValueError("PostgreSQL auto.conf differs from the sealed PITR start inputs")
        return
    expected = dict(request.configuration_files)
    accepted_env = {expected[".env"]}
    if record is not None:
        if record.rollback_expected_env_digest is not None:
            accepted_env.add(record.rollback_expected_env_digest)
        intent = record.config_apply_intent
        if intent is not None and intent["kind"] == "env":
            accepted_env.update((intent["expected_digest"], intent["desired_digest"]))
    if observed[".env"] not in accepted_env:
        raise ValueError("PITR environment changed outside its exact owned write")
    expected[".env"] = observed[".env"]
    if observed != expected:
        raise ValueError("non-PITR startup configuration changed after reservation")
