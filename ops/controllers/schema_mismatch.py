"""Read-only code/schema/pin diagnosis and the watchdog's local blocked-round record."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

import shared.db
from shared.api_contracts.status import SchemaMismatchStatus
from shared.cluster_pin import get_cluster_target_sha
from shared.machine import MachineRole, machine_name
from shared.migration_errors import MigrationLayoutError
from shared.migration_layout import required_migration_set, required_migration_set_at_ref
from shared.migrations import applied_migration_names
from shared.paths import ava_home

_log = logging.getLogger(__name__)
SchemaKind = Literal["pin-behind-schema", "schema-ahead-of-code", "schema-behind-code", "divergent"]


@dataclass(frozen=True)
class Mismatch:
    kind: SchemaKind
    detail: str
    signature: str


class _BlockedRecord(TypedDict):
    signature: str
    rounds: int
    held_back_services: list[str]


def classify(
    applied: set[str], required: set[str], pin_required: set[str] | None, pin: str | None
) -> Mismatch | None:
    """Prefer the cluster-wide pin split over a local code mismatch."""
    pin_extra: set[str] = applied - pin_required if pin_required is not None else set()
    local_extra = applied - required
    local_missing = required - applied
    if pin_extra:
        kind: SchemaKind = "pin-behind-schema"
        detail = (
            f"cluster pin {pin[:7] if pin else '(unset)'} lacks {len(pin_extra)} applied "
            "DB migration(s); run pin-aware `ava cluster update` on the gateway"
        )
    elif local_extra and local_missing:
        kind = "divergent"
        detail = (
            f"DB has {len(local_extra)} migration(s) this code lacks and lacks "
            f"{len(local_missing)} required by this code"
        )
    elif local_extra:
        kind = "schema-ahead-of-code"
        detail = f"DB has {len(local_extra)} migration(s) this host's code lacks"
    elif local_missing:
        kind = "schema-behind-code"
        detail = f"DB lacks {len(local_missing)} migration(s) required by this host's code"
    else:
        return None
    facts = json.dumps(
        [kind, pin, sorted(applied), sorted(required), sorted(pin_required or ())],
        separators=(",", ":"),
    )
    return Mismatch(kind, detail, hashlib.sha256(facts.encode()).hexdigest())


def detect() -> Mismatch | None:
    """Compare the live applied set with local code and the cluster pin tree."""
    try:
        with shared.db.connect(autocommit=True) as conn:
            applied = applied_migration_names(conn)
        pin = get_cluster_target_sha()
    except Exception as exc:
        # This is an observational second read after the schema controller's
        # authoritative gate. A sick DB must not prevent DB-free healthchecks.
        _log.warning("[schema-mismatch] DB facts unavailable: %r", exc)
        return None
    try:
        required = required_migration_set()
        pin_required = required_migration_set_at_ref(pin) if pin is not None else None
    except MigrationLayoutError as exc:
        detail = f"migration tree could not be read: {exc}"
        return Mismatch("divergent", detail, hashlib.sha256(detail.encode()).hexdigest())
    return classify(applied, required, pin_required, pin)


def _path(role: MachineRole) -> Path:
    return ava_home() / f"schema-block-{role}.json"


def _read(role: MachineRole) -> _BlockedRecord | None:
    try:
        value = json.loads(_path(role).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("[schema-mismatch] unreadable watchdog state for %s: %s", role, exc)
        return None
    if not isinstance(value, dict):
        _log.warning("[schema-mismatch] invalid watchdog state for %s", role)
        return None
    record = cast(dict[str, object], value)
    if record.get("state") == "clear":
        return None
    services = record.get("held_back_services")
    if (
        not isinstance(record.get("signature"), str)
        or type(record.get("rounds")) is not int
        or not isinstance(services, list)
        or not all(isinstance(service, str) for service in cast(list[object], services))
    ):
        _log.warning("[schema-mismatch] invalid watchdog state for %s", role)
        return None
    return cast(_BlockedRecord, record)


def _write(role: MachineRole, value: dict[str, object]) -> None:
    path = _path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=".schema-block-", suffix=".tmp")
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, separators=(",", ":"), sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)  # noqa: PTH105 — atomic status publication
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def observe(role: MachineRole, mismatch: Mismatch, held_back: list[str]) -> int:
    """Count consecutive schema-blocked rounds for one watchdog capability."""
    previous = _read(role)
    rounds = previous["rounds"] + 1 if previous is not None else 1
    _write(
        role,
        {
            "signature": mismatch.signature,
            "rounds": rounds,
            "held_back_services": held_back,
        },
    )
    return rounds


def clear(role: MachineRole) -> None:
    """End an observed block; a later drift begins a new streak."""
    if _read(role) is not None:
        _write(role, {"state": "clear"})


def status() -> SchemaMismatchStatus | None:
    """Fresh mismatch facts plus the last watchdog count and held-back roster."""
    mismatch = detect()
    if mismatch is None:
        return None
    matching = [
        row
        for role in ("gateway", "agent-runner")
        if (row := _read(role)) is not None and row.get("signature") == mismatch.signature
    ]
    rounds = max((row["rounds"] for row in matching), default=0)
    held = sorted({service for row in matching for service in row["held_back_services"]})
    return SchemaMismatchStatus(
        kind=mismatch.kind,
        machine=machine_name(),
        consecutive_blocked_rounds=rounds,
        held_back_services=held,
        detail=mismatch.detail,
    )
