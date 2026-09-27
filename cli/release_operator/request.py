"""`ava cluster release request` — build one single-host release `Request`.

This closes the fleet-and-cutover plan's gap 4 ("no operator Request
builder") for exactly one host: it builds the same
`cli.release_transition.request.Request` that
`scripts/preview/release_cycle_runtime.py::prepare` used to build only for
the preview's own private use, from a real home's currently selected release
and a real `ava cluster release prepare` receipt. The written file is meant
to be handed straight to the existing `ava cluster update --prepared`.

`--exclude`/`--reason` name a multi-host fleet exclusion. There is no fleet
request model yet (`cli/release_fleet/`, slice FC-7); this verb refuses
rather than approximate one.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cli.release_fleet.request import FleetRequest
from cli.release_operator.current import current_release
from cli.release_operator.layout import receipt_path, require_commit_shape
from cli.release_prepare.models import PreparationReceipt
from cli.release_transition.request import ReleaseRef, verify_pair
from shared.verified_file import regular_bytes


def _candidate_from_receipt(home: Path, commit: str) -> ReleaseRef:
    path = receipt_path(home, commit)
    try:
        encoded = regular_bytes(path)
    except FileNotFoundError:
        raise ValueError(
            f"no prepared receipt at {path} — run "
            f"`ava cluster release prepare --commit {commit}` on this host first"
        ) from None
    receipt = PreparationReceipt.model_validate_json(encoded)
    if receipt.source.source_commit != commit:
        raise ValueError("prepared receipt names a different commit than requested")
    return ReleaseRef(
        artifact_digest=receipt.image.artifact_digest,
        manifest_digest=receipt.image.manifest_digest,
        schema_digest=receipt.image.schema_digest,
        source_commit=receipt.source.source_commit,
    )


def _build_request(*, commit: str, exclude: tuple[str, ...], reason: str | None) -> FleetRequest:
    from shared.cluster import registry_path
    from shared.machine import machine_name
    from shared.paths import ava_home
    from shared.start_inputs import configuration_digest

    if exclude or reason is not None:
        raise ValueError(
            "--exclude/--reason name a multi-host fleet exclusion; this single-host "
            "slice builds no FleetRequest — see FC-7 (fleet-core)"
        )
    require_commit_shape(commit)
    home = ava_home()
    candidate = _candidate_from_receipt(home, commit)
    found = current_release(home)
    if found is None:
        raise ValueError(
            "this home has no active release selection yet — run `ava cluster release adopt` first"
        )
    previous, _ = found
    if previous.selector == candidate.selector:
        raise ValueError("the prepared candidate is already this home's active release")
    request = FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(registry_path()),
        created_at=datetime.now(UTC),
        machine=machine_name(),
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest=configuration_digest(home),
    )
    verify_pair(request)
    return request


def _write_request(out: Path, request: FleetRequest) -> None:
    encoded = (request.model_dump_json() + "\n").encode()
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"{out} already exists") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def cmd_release_request(
    *, commit: str, out: Path, exclude: tuple[str, ...], reason: str | None
) -> int:
    try:
        request = _build_request(commit=commit, exclude=exclude, reason=reason)
        _write_request(out, request)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release request refused: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps({"request": str(out), "operation": str(request.path), "id": str(request.id)})
        + "\n"
    )
    return 0
