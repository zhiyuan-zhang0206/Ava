"""Startup admission while a finite release executor owns a home's transition.

The operation journal is the only decision record. This settings-free reader
does not import CLI orchestration or infer permission from an environment flag.
An explicit in-process capability permits one exact journal revision to start;
children and an ordinary operator invocation do not inherit that capability.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251 — explicit start capability
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from shared.start_inputs import require_configuration
from shared.verified_file import regular_bytes

_start: ContextVar[tuple[Path, str] | None] = ContextVar("release_start", default=None)
_pitr: ContextVar[tuple[Path, Path, str, int, Callable[[bytes | None, bytes], None]] | None] = (
    ContextVar("pitr_record_writer", default=None)
)


def require_pitr_authorized(home: Path) -> None:
    """Configuration effects require the admitted executor's local capability."""
    current = _pitr.get()
    if current is None or current[0] != home:
        raise RuntimeError("PITR configuration requires the home operation executor")
    active = _active(home)
    if active is None or active[0] != current[1]:
        raise RuntimeError("PITR home operation capability changed")
    operation = json.loads(active[1])
    _require_operation_identity(home, active[0], operation)
    if (
        operation["request"]["kind"] != "pitr"
        or operation["request"]["home"] != str(home)
        or operation["pitr"]["action"] != current[2]
        or operation["pitr"]["generation"] != current[3]
        or operation["phase"] not in {"provisioning", "proving"}
    ):
        raise RuntimeError("PITR action generation or effect phase changed")


def require_configuration_write_authorized(home: Path) -> None:
    """A finite operation owns its captured settings until its terminal outcome."""
    active = _active(home)
    if active is None:
        return
    operation = json.loads(active[1])
    _require_operation_identity(home, active[0], operation)
    if operation["phase"] == "complete" and operation["error"] is None:
        return
    require_pitr_authorized(home)
    if operation["phase"] != "provisioning":
        raise RuntimeError("PITR settings writes require the provisioning phase")


def note_pitr_write(home: Path, before: bytes | None, after: bytes) -> None:
    """Publish business-record digests before a write under an active PITR owner."""
    current = _pitr.get()
    if current is not None:
        require_pitr_authorized(home)
        current[4](before, after)
        return
    active = _active(home)
    if active is not None:
        raise RuntimeError("active home business record requires its PITR executor capability")


@contextmanager
def authorized_pitr(
    path: Path, before_write: Callable[[bytes | None, bytes], None]
) -> Generator[None]:
    """Granted only after native executor admission while holding the home lock."""
    operation = json.loads(regular_bytes(path))
    request, progress = operation["request"], operation["pitr"]
    if request["kind"] != "pitr" or progress is None:
        raise ValueError("PITR capability requires a typed PITR operation")
    token = _pitr.set(
        (Path(request["home"]), path, progress["action"], progress["generation"], before_write)
    )
    try:
        yield
    finally:
        _pitr.reset(token)


def _active(home: Path) -> tuple[Path, bytes] | None:
    pointer = home / "updates" / "active"
    try:
        path = Path(regular_bytes(pointer).decode().strip())
    except FileNotFoundError:
        return None
    if (
        path.parent.parent != home / "updates"
        or path.name != "operation.json"
        or str(UUID(path.parent.name)) != path.parent.name
        or path.resolve(strict=True) != path
    ):
        raise ValueError("invalid active release operation binding")
    return path, regular_bytes(path, max_bytes=256 * 1024)


def _require_operation_identity(home: Path, path: Path, operation: dict[str, Any]) -> None:
    request = operation["request"]
    if (
        request["version"] != 1
        or request["home"] != str(home)
        or request["id"] != path.parent.name
        or request["kind"] not in {"release", "pitr"}
    ):
        raise ValueError("invalid active release operation identity")
    if request["kind"] == "release":
        if operation["direction"] not in {"candidate", "previous"} or operation["pitr"] is not None:
            raise ValueError("invalid release operation progress identity")
    elif operation["direction"] is not None or operation["pitr"] is None:
        raise ValueError("invalid PITR operation progress")


def require_start_authorized(home: Path) -> tuple[str, datetime] | None:
    """Return the exact operation hold, or no hold for ordinary startup.

    The same admitted bytes authorize maintenance after identity preparation;
    this boundary itself never loads runtime configuration. An interrupted
    update remains held across operator start and OS reboot.
    """
    active = _active(home)
    if active is None:
        return None
    path, encoded = active
    operation = json.loads(encoded)
    _require_operation_identity(home, path, operation)
    request = operation["request"]
    if operation["phase"] == "complete" and operation["error"] is None:
        return None
    if operation["phase"] == "starting" and _start.get() == (
        path,
        hashlib.sha256(encoded).hexdigest(),
    ):
        digest = request["configuration_digest"]
        if request["kind"] == "pitr":
            seal = operation["pitr"]["seal"]
            if seal is None:
                raise ValueError("PITR start requires its provisioned configuration seal")
            digest = seal["configuration_digest"]
        require_configuration(home, digest)
        at = datetime.fromisoformat(
            request["created_at"]
            if request["kind"] == "release"
            else operation["pitr"]["maintenance_at"]
        )
        if at.tzinfo is None:
            raise ValueError("release start requires an aware maintenance timestamp")
        return request["id"], at
    raise RuntimeError(
        f"release operation {path.parent.name} holds startup at {operation['phase']}"
    )


@contextmanager
def authorized_start(path: Path) -> Generator[None]:
    """Used by the admitted native start action after its operation checks."""
    encoded = regular_bytes(path, max_bytes=256 * 1024)
    token = _start.set((path, hashlib.sha256(encoded).hexdigest()))
    try:
        yield
    finally:
        _start.reset(token)
