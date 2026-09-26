"""One durable operation per home; intent precedes every external effect.

The native executor owns execution. This journal owns release decisions and
progress, never process supervision. A stopped controller cannot turn an
uncertain native effect into permission to repeat it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from cli.release_transition.pitr_evidence import PitrProgress, PitrSeal
from cli.release_transition.request import AnyRequest, PitrRequest, Record, ReleaseRef, Request
from shared.atomic_io import write_text_atomic
from shared.native_process.ownership import OwnedProcess
from shared.platform import file_lock
from shared.private_storage import ensure_private_dir, private_file_problem
from shared.process_evidence import ExpectedProcess
from shared.runtime_release import current_pointer
from shared.verified_file import regular_bytes

Phase = Literal[
    "prepared",
    "quiescing",
    "stopping",
    "selecting",
    "starting",
    "observing",
    "resuming",
    "complete",
    "provisioning",
    "stopping_apps",
    "stopping_data",
    "proving",
]
Direction = Literal["candidate", "previous"]
_MAX_JOURNAL_BYTES = 256 * 1024
_DARWIN_KIND = "darwin-launchd-v1"
_NEXT: dict[str, str] = {
    "prepared": "quiescing",
    "quiescing": "stopping",
    "stopping": "selecting",
    "selecting": "starting",
    "starting": "observing",
    "observing": "resuming",
    "resuming": "complete",
}
_PITR_NEXT: dict[str, str] = {
    "prepared": "provisioning",
    "provisioning": "quiescing",
    "quiescing": "stopping_apps",
    "stopping_apps": "stopping_data",
    "stopping_data": "starting",
    "starting": "observing",
    "observing": "resuming",
    "resuming": "proving",
    "proving": "complete",
}


class Retirement(Record):
    terminal: dict[str, JsonValue]
    state: Literal["requested", "absent"] = "requested"


class Operation(Record):
    request: AnyRequest
    revision: int = Field(default=0, ge=0)
    phase: Phase = "prepared"
    direction: Direction | None = "candidate"
    pitr: PitrProgress | None = None
    launch: dict[str, JsonValue] | None = None
    launch_attempted: bool = False
    native: dict[str, JsonValue] | None = None
    retirement: Retirement | None = None
    attempt: int = Field(default=0, ge=0)
    retired_executors: tuple[dict[str, JsonValue], ...] = ()
    error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def coherent_kind(self) -> Self:
        if isinstance(self.request, PitrRequest):
            if self.direction is not None or self.pitr is None:
                raise ValueError("PITR requires its own action progress, never release direction")
            if self.phase not in {*_PITR_NEXT, "complete"}:
                raise ValueError("PITR cannot enter a release-only phase")
        elif (
            self.pitr is not None
            or self.direction is None
            or self.phase not in {*_NEXT, "complete"}
        ):
            raise ValueError("release requires only release progress")
        return self

    @model_validator(mode="after")
    def coherent_attempts(self) -> Self:
        if self.attempt != len(self.retired_executors) or any(
            retired["attempt"] != index for index, retired in enumerate(self.retired_executors)
        ):
            raise ValueError("executor attempts require an ordered, complete closure history")
        if self.launch_attempted and self.launch is None:
            raise ValueError("native dispatch requires retained launch intent")
        if (self.native is not None or self.retirement is not None) and not self.launch_attempted:
            raise ValueError("native identity and retirement require recorded dispatch")
        return self

    @property
    def terminal(self) -> bool:
        return self.phase == "complete"

    @property
    def reference(self) -> ReleaseRef:
        if isinstance(self.request, PitrRequest):
            return self.request.image
        return self.request.candidate if self.direction == "candidate" else self.request.previous

    @property
    def maintenance_at(self) -> datetime:
        return self.request.created_at if self.pitr is None else self.pitr.maintenance_at

    def require_configuration(self) -> None:
        if self.pitr is None or self.pitr.seal is None:
            self.request.require_configuration()
            return
        from shared.start_inputs import require_configuration

        require_configuration(Path(self.request.home), self.pitr.seal.configuration_digest)


def read_operation(path: Path) -> Operation:
    operation = Operation.model_validate_json(regular_bytes(path, max_bytes=_MAX_JOURNAL_BYTES))
    if operation.request.path != path or path.resolve(strict=True) != path:
        raise ValueError("operation journal is not at its captured home and generation")
    return operation


def _write(operation: Operation) -> None:
    encoded = operation.model_dump_json() + "\n"
    if len(encoded.encode("utf-8")) > _MAX_JOURNAL_BYTES:
        raise ValueError("release journal capacity exceeded; retained state was not changed")
    write_text_atomic(
        operation.request.path,
        encoded,
        mode=0o600,
        sync_file=True,
        sync_parent=True,
    )


def _lock(home: Path) -> Path:
    path = home / "updates" / "operation.lock"
    if problem := private_file_problem(path):
        raise ValueError(f"release operation lock {problem}")
    return path


def _initial_operation(request: Request | PitrRequest) -> Operation:
    """Publish a new intent only while create holds the home operation lock."""
    # Initial admission and every executor share this home lock. A predecessor
    # checked by the caller can change before lock entry.
    reference = request.image if isinstance(request, PitrRequest) else request.previous
    if current_pointer(Path(request.home) / "releases") != reference.selector:
        raise ValueError("prepared predecessor is not the selected release")
    if isinstance(request, PitrRequest):
        from services.pitr.activation_state import record_path

        try:
            digest = hashlib.sha256(regular_bytes(record_path(Path(request.home)))).hexdigest()
        except FileNotFoundError:
            digest = None
        if digest != request.expected_record:
            raise ValueError("activation record changed before home reservation")
        request.require_configuration()
    ensure_private_dir(request.path.parent)
    if isinstance(request, PitrRequest):
        operation = Operation(
            request=request,
            direction=None,
            pitr=PitrProgress(
                action=request.action,
                generation=request.generation,
                maintenance_at=request.created_at,
                record_digest=request.expected_record,
            ),
        )
    else:
        operation = Operation(request=request)
    _write(operation)
    return operation


def create(request: Request | PitrRequest) -> Operation:
    """Allocate once; exact retries recover the same journal without resetting it."""
    home = Path(request.home)
    if home.resolve(strict=True) != home:
        raise ValueError("release operation home is not canonical")
    directory = ensure_private_dir(home / "updates")
    with file_lock(_lock(home), timeout_s=5):
        active = directory / "active"
        try:
            active_path = Path(regular_bytes(active).decode().strip())
        except FileNotFoundError:
            active_path = None
        if active_path is not None:
            prior = read_operation(active_path)
            if prior.request.home != request.home:
                raise ValueError("active release operation belongs to another home")
            if prior.request.id == request.id:
                if prior.request != request:
                    raise ValueError("an existing release operation cannot change its inputs")
                return prior
            if not prior.terminal:
                raise ValueError("another release operation remains incomplete")
            if prior.launch_attempted and (
                prior.retirement is None or prior.retirement.state != "absent"
            ):
                raise ValueError("previous release executor has not completed native retirement")
        # A crash between the journal and active pointer is recoverable without
        # overwriting any possibly executed generation.
        if request.path.exists() or request.path.is_symlink():
            ensure_private_dir(request.path.parent)
            operation = read_operation(request.path)
            if operation.request != request:
                raise ValueError("retained release operation inputs differ")
        else:
            operation = _initial_operation(request)
        write_text_atomic(active, str(request.path) + "\n", mode=0o600, sync_parent=True)
        return operation


def _native_process(record: dict[str, JsonValue]) -> OwnedProcess:
    evidence = ExpectedProcess.model_validate(
        {
            "pid": record["pid"],
            "create_time": record["birth"],
            "starttime": record["starttime"],
        }
    )
    return OwnedProcess(evidence.pid, evidence.create_time, evidence.starttime)


def _darwin(launch: dict[str, JsonValue]) -> bool:
    """macOS launches carry their kind; every other record keeps the systemd contract."""
    return "kind" in launch and launch["kind"] == _DARWIN_KIND


def _same_native_receipt(
    launch: dict[str, JsonValue], before: dict[str, JsonValue], after: dict[str, JsonValue]
) -> bool:
    if _darwin(launch):
        # macOS births are the kernel's monotonic start readings: exact only.
        return False
    # Only Linux's derived wall time may differ. Boot, invocation, cgroup and
    # every other captured attribute retain exact equality across exec/replay.
    if (before | {"birth": None}) != (after | {"birth": None}):
        return False
    return _native_process(before).same_birth(_native_process(after))


def _require_linux_closure(
    launch: dict[str, JsonValue],
    native: dict[str, JsonValue] | None,
    terminal: dict[str, JsonValue],
) -> None:
    if terminal["owner"] is not None or terminal["sub"] not in {"exited", "failed", "dead"}:
        raise ValueError("a living or unknown executor cannot be replaced")
    for key in ("unit", "boot_id"):
        if terminal[key] != launch[key]:
            raise ValueError("closure belongs to a different native executor")
    if native is not None and any(
        terminal[key] != native[key] for key in ("unit", "boot_id", "invocation_id", "cgroup")
    ):
        raise ValueError("closure changed the recorded executor identity")


def _require_darwin_closure(
    launch: dict[str, JsonValue],
    native: dict[str, JsonValue] | None,
    terminal: dict[str, JsonValue],
) -> None:
    """Terminal launchd facts plus recorded births closed; helper and executor stay distinct."""
    ended = (terminal["kind"], terminal["state"], terminal["runs"])
    if ended != (_DARWIN_KIND, "not running", 1) or (terminal["helper"], terminal["executor"]) != (
        None,
        None,
    ):
        raise ValueError("a living or unknown executor cannot be replaced")
    if any(terminal[key] != launch[key] for key in ("label", "domain", "boot_id")):
        raise ValueError("closure belongs to a different native executor")
    closed = (
        None if native is None else {"helper": native["helper"], "executor": native["executor"]}
    )
    if terminal["closed"] != closed or (native is not None and terminal["asid"] != native["asid"]):
        raise ValueError("closure changed the recorded executor identity")


def _require_closure(
    launch: dict[str, JsonValue],
    native: dict[str, JsonValue] | None,
    terminal: dict[str, JsonValue],
) -> None:
    if _darwin(launch):
        _require_darwin_closure(launch, native, terminal)
    else:
        _require_linux_closure(launch, native, terminal)


class Journal:
    """Mutations require the home-wide lock through the entire executor run."""

    def __init__(self, operation: Operation) -> None:
        self.operation = operation

    def _replace(self, **changes: object) -> Operation:
        path = self.operation.request.path
        current = read_operation(path)
        if current != self.operation:
            raise ValueError("release operation changed while executing")
        updated = Operation.model_validate(
            self.operation.model_dump() | changes | {"revision": current.revision + 1}
        )
        _write(updated)
        self.operation = updated
        return updated

    def record_launch(self, record: dict[str, JsonValue]) -> Operation:
        if self.operation.launch is not None:
            if self.operation.launch != record:
                raise ValueError("an operation cannot replace its recorded native launch")
            return self.operation
        if self.operation.terminal or (
            self.operation.phase != "prepared" and self.operation.attempt == 0
        ):
            raise ValueError("native launch must be captured before release effects")
        return self._replace(launch=record)

    def advance(self, phase: Phase) -> Operation:
        next_phases = _PITR_NEXT if self.operation.pitr is not None else _NEXT
        if next_phases.get(self.operation.phase) != phase:
            raise ValueError(f"invalid release transition {self.operation.phase} -> {phase}")
        return self._replace(phase=phase, error=None)

    def record_pitr(self, progress: PitrProgress) -> Operation:
        if self.operation.pitr is None or self.operation.terminal:
            raise ValueError("PITR effects require an active PITR operation")
        current = self.operation.pitr
        if (progress.action, progress.generation) != (current.action, current.generation):
            raise ValueError("PITR action changes require an explicit rollback decision")
        return self._replace(pitr=progress)

    def pitr_record_write(self, before: bytes | None, after: bytes) -> None:
        """Retain exact cross-file write intent, not a second activation record."""
        progress = self.operation.pitr
        if progress is None:
            raise ValueError("business mutation requires a PITR operation")
        digest = None if before is None else hashlib.sha256(before).hexdigest()
        accepted = {progress.record_digest}
        if progress.record_intent is not None:
            accepted.add(progress.record_intent[1])
        if digest not in accepted:
            raise ValueError("activation record changed outside the captured PITR action")
        updated = progress.model_copy(
            update={
                "record_digest": digest,
                "record_intent": (digest, hashlib.sha256(after).hexdigest()),
            }
        )
        self.record_pitr(updated)

    def provisioned(self, seal: PitrSeal, *, data_stopped: bool = False) -> Operation:
        progress = self.operation.pitr
        if progress is None or self.operation.phase != "provisioning":
            raise ValueError("PITR provisioning seal requires the provisioning boundary")
        if data_stopped and progress.data_stop is None:
            raise ValueError("offline continuation requires retained data stop custody")
        updated = progress.model_copy(
            update={
                "seal": seal,
                "record_digest": seal.activation_digest,
                "record_intent": None,
                "data_stop": progress.data_stop if data_stopped else None,
            }
        )
        return self._replace(
            pitr=updated, phase="starting" if data_stopped else "quiescing", error=None
        )

    def pitr_no_restart(self) -> Operation:
        if self.operation.pitr is None or self.operation.phase != "provisioning":
            raise ValueError("no-restart completion requires PITR provisioning")
        return self._replace(phase="complete", error=None)

    def decide_rollback(self, record_digest: str, *, maintenance_at: datetime) -> Operation:
        current = self.operation
        progress = current.pitr
        if progress is None or current.terminal or progress.action == "rollback":
            raise ValueError("rollback requires an incomplete activation")
        if current.launch_attempted and (
            current.retirement is None or current.retirement.state != "absent"
        ):
            raise ValueError("rollback decision requires exact native executor retirement")
        decision = {
            "generation": progress.generation,
            "action": progress.action,
            "phase": current.phase,
            "record_digest": record_digest,
        }
        updated = progress.model_copy(
            update={
                "action": "rollback",
                "generation": progress.generation + 1,
                "record_digest": record_digest,
                "record_intent": None,
                "decisions": (*progress.decisions, decision),
                "seal": None,
                "maintenance_at": maintenance_at,
            }
        )
        return self._replace(
            pitr=updated,
            phase="provisioning" if current.launch_attempted else "prepared",
            error=None,
        )

    def mark_launch_attempted(self) -> Operation:
        if self.operation.launch is None or self.operation.launch_attempted:
            raise ValueError("native dispatch requires one previously unattempted launch intent")
        if self.operation.terminal or (
            self.operation.phase != "prepared" and self.operation.attempt == 0
        ):
            raise ValueError("native dispatch must precede release effects")
        return self._replace(launch_attempted=True)

    def record_native(self, record: dict[str, JsonValue]) -> Operation:
        if not self.operation.launch_attempted:
            raise ValueError("native birth cannot precede native dispatch")
        if self.operation.native is not None:
            launch = self.operation.launch
            if launch is None:
                raise ValueError("native identity requires retained launch intent")
            if self.operation.native != record and not _same_native_receipt(
                launch, self.operation.native, record
            ):
                raise ValueError("native executor identity changed")
            return self.operation
        return self._replace(native=record)

    def request_retirement(self, terminal: dict[str, JsonValue]) -> Operation:
        """Retain closure and deletion intent before retiring a native unit."""
        current = self.operation
        if current.launch is None or not current.launch_attempted:
            raise ValueError("native retirement requires an attempted launch")
        _require_closure(current.launch, current.native, terminal)
        if current.retirement is not None:
            if current.retirement.terminal != terminal:
                raise ValueError("native retirement cannot change its closure evidence")
            return current
        return self._replace(retirement=Retirement(terminal=terminal))

    def record_retired(self) -> Operation:
        """The adapter proved the exact native job and its admitted closure scope absent."""
        retirement = self.operation.retirement
        if retirement is None:
            raise ValueError("native absence requires a recorded deletion intent")
        if retirement.state == "absent":
            return self.operation
        return self._replace(retirement=Retirement(terminal=retirement.terminal, state="absent"))

    def relaunch(self, terminal: dict[str, JsonValue]) -> Operation:
        """Continue the same decision after native closure and exact retirement.

        A crash here leaves one unattempted, resumable intent. Archived
        attempts have no pending cleanup; the former native definition is gone.
        """
        current = self.operation
        if current.terminal or current.launch is None or not current.launch_attempted:
            raise ValueError("only an attempted incomplete operation can continue")
        if (
            current.retirement is None
            or current.retirement.state != "absent"
            or current.retirement.terminal != terminal
        ):
            raise ValueError("continuation requires the exact completed native retirement")
        retired: dict[str, JsonValue] = {
            "attempt": current.attempt,
            "launch": current.launch,
            "native": current.native,
            "terminal": terminal,
            "retirement": current.retirement.model_dump(mode="json"),
        }
        return self._replace(
            attempt=current.attempt + 1,
            retired_executors=(*current.retired_executors, retired),
            launch=None,
            launch_attempted=False,
            native=None,
            retirement=None,
        )

    def recover(self, reason: str) -> Operation:
        if self.operation.pitr is not None:
            raise ValueError("PITR has no automatic release-image rollback")
        if self.operation.terminal or self.operation.direction == "previous":
            raise ValueError("a recovery cannot recover again or reverse a completed decision")
        if self.operation.phase not in {"starting", "observing"}:
            raise ValueError("only candidate startup or observation can choose recovery")
        return self._replace(direction="previous", phase="stopping", error=reason)

    def fail(self, detail: str) -> Operation:
        """Retain the uncertain phase; an exception is never a closure receipt."""
        if self.operation.terminal:
            raise ValueError("a completed release operation cannot become failed")
        return self._replace(error=detail)


@contextmanager
def exclusive(path: Path) -> Generator[Journal]:
    operation = read_operation(path)
    home = Path(operation.request.home)
    with file_lock(_lock(home), timeout_s=5):
        active = Path(regular_bytes(home / "updates" / "active").decode().strip())
        if active != path:
            raise ValueError("this is not the home's active release operation")
        yield Journal(read_operation(path))
