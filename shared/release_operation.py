"""Startup admission while a finite release executor owns a home's transition.

The operation journal is the only decision record. This settings-free reader
does not import CLI orchestration or infer permission from an environment flag.
An explicit in-process capability permits one exact journal revision to start;
children and an ordinary operator invocation do not inherit that capability.
The running executor's heartbeat beside the journal is the only proof it
still runs: an incomplete operation explains an outage only while it is fresh.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251 — explicit start capability
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
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
        or request["kind"] not in {"fleet", "unit", "pitr"}
    ):
        raise ValueError("invalid active release operation identity")
    if request["kind"] != "pitr":
        if (
            operation["direction"] not in {"candidate", "previous"}
            or operation["pitr"] is not None
            or operation[request["kind"]] is None
        ):
            raise ValueError("invalid release operation progress identity")
    elif operation["direction"] is not None or operation["pitr"] is None:
        raise ValueError("invalid PITR operation progress")


# How an operator reads each journaled kind: a fleet operation is the release.
_KIND_LABELS = {"fleet": "release", "unit": "unit release", "pitr": "pitr"}
# Beside the journal: the running executor's last sign of life (`executor_heartbeat`).
_HEARTBEAT = "executor-heartbeat"
_MAX_HEARTBEAT_BYTES = 128


class InFlight(NamedTuple):
    """This home's incomplete operation, and whether its executor provably lives.

    `last_seen` is the executor's last heartbeat or, before its first one, the
    launch-grace stamp its submission and each native dispatch leave
    (`open_launch_grace`); with no stamp at all, the request's creation.
    `alive` means that is at most `EXECUTOR_HEARTBEAT_TTL_S` old.
    `recovering`: the operation journaled an abort, recovery or rollback
    decision. `failed`: it recorded an error — a hold, whose executor exited
    on purpose, or a decision's error until its next phase.
    """

    label: str
    alive: bool
    last_seen: datetime
    recovering: bool
    failed: bool

    @property
    def explains(self) -> bool:
        """Whether it explains an outage: a live executor on the planned path.

        A recovering operation's hold is itself worth an alert, in every
        phase after its decision, not only while the decision's error is the
        journal's latest. A failed one explains nothing: its hold is itself
        worth an alert.
        """
        return self.alive and not self.recovering and not self.failed

    @property
    def lost(self) -> bool:
        """Its executor stopped stamping without recording a failure."""
        return not self.alive and not self.failed


def operation_in_flight(home: Path, *, now: datetime | None = None) -> InFlight | None:
    """This home's incomplete operation, if any; None when none is active or it completed.

    It explains an outage of the unit it is replacing only while its executor
    is alive (`InFlight.explains`): a killed, OOM'd or rebooted executor, or
    one that never launched, stops stamping its heartbeat, and its operation
    then explains nothing — the executor is lost. A recovering operation
    explains nothing either, nor does a failed one (a recorded error): a held
    operation's executor exited on purpose, so it is never lost, and its hold
    is itself worth an alert.
    """
    from shared.deploy_timing import EXECUTOR_HEARTBEAT_TTL_S

    active = _active(home)
    if active is None:
        return None
    path, encoded = active
    operation = json.loads(encoded)
    _require_operation_identity(home, path, operation)
    if operation["phase"] == "complete":
        return None
    kind = operation["request"]["kind"]
    created = datetime.fromisoformat(operation["request"]["created_at"])
    last_seen = max(created, _heartbeat(path) or created)
    stale = (now or datetime.now(UTC)) - last_seen > timedelta(seconds=EXECUTOR_HEARTBEAT_TTL_S)
    return InFlight(
        label=f"{_KIND_LABELS[kind]} operation {path.parent.name} at {operation['phase']}",
        alive=not stale,
        last_seen=last_seen,
        recovering=bool(operation[kind]["decisions"]),
        failed=operation["error"] is not None,
    )


def _heartbeat(path: Path) -> datetime | None:
    """The executor's last stamp; a missing or unreadable one is no evidence of life.

    Stamps are replaced atomically, so the file opened is always one complete
    stamp, even when the next one lands while it is read.
    """
    try:
        fd = os.open(path.parent / _HEARTBEAT, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            stamped = datetime.fromisoformat(stream.read(_MAX_HEARTBEAT_BYTES).decode().strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return stamped if stamped.tzinfo is not None else None


def open_launch_grace(path: Path) -> None:
    """Stamp operation `path`'s heartbeat on its executor's behalf: the launch grace.

    The submission stamps it once the journal exists, and each native dispatch
    (a continuation's included) again, so the grace never runs from the
    request's own `created_at`, which `release request` stamped any time
    before. The executor's own beats replace it once it runs.
    """
    from shared.atomic_io import write_text_atomic

    write_text_atomic(path.parent / _HEARTBEAT, datetime.now(UTC).isoformat() + "\n", mode=0o600)


@contextmanager
def executor_heartbeat(path: Path) -> Generator[None]:
    """Stamp operation `path`'s executor heartbeat until the executor leaves.

    One stamp before the body and then one every `LEASE_RENEW_INTERVAL_S`
    from a thread; a failed stamp is only a missed beat. Leaving removes the
    stamp, so an executor that exits without recording an outcome leaves no
    evidence of life behind; a process that dies here simply stops stamping.
    """
    from shared.atomic_io import write_text_atomic
    from shared.deploy_timing import LEASE_RENEW_INTERVAL_S

    beat = path.parent / _HEARTBEAT
    done = threading.Event()

    def stamp() -> None:
        write_text_atomic(beat, datetime.now(UTC).isoformat() + "\n", mode=0o600)

    def beating() -> None:
        while not done.wait(LEASE_RENEW_INTERVAL_S):
            try:
                stamp()
            except OSError as exc:
                from shared.log import logger

                logger.warning("[release] executor heartbeat missed a beat: {exc}", exc=exc)

    stamp()
    thread = threading.Thread(target=beating, name="executor-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
        beat.unlink(missing_ok=True)


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
    # `restoring` is an abort's restart of the unchanged previous image.
    if operation["phase"] in {"starting", "restoring"} and _start.get() == (
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
        at = datetime.fromisoformat(operation[request["kind"]]["maintenance_at"])
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
