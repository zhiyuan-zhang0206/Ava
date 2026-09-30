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
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from base.deploy.release.runtime_release import current_pointer
from base.deploy.release.verified_file import regular_bytes
from base.host.atomic_io import write_text_atomic
from base.host.private_storage import ensure_private_dir, private_file_problem
from base.native_process.evidence import ExpectedProcess
from base.native_process.os_platform import file_lock
from base.native_process.ownership import OwnedProcess
from cli.release_fleet.progress import (
    FLEET_PHASES,
    UNIT_PHASES,
    Decision,
    FleetProgress,
    Outcome,
    UnitProgress,
    decision_target,
    initial_progress,
    next_phase,
)
from cli.release_fleet.request import FleetRequest, UnitRequest
from cli.release_transition.authority_evidence import Fence, Issue, require_coherent
from cli.release_transition.native import DARWIN, LINUX
from cli.release_transition.pitr.evidence import PitrProgress, PitrSeal
from cli.release_transition.request import PitrRequest, Record, ReleaseRef

Phase = Literal[
    "prepared",
    "quiescing",
    "stopping",
    "fencing",
    "selecting",
    "authorizing",
    "starting",
    "observing",
    "resuming",
    "complete",
    "provisioning",
    "stopping_apps",
    "stopping_data",
    "proving",
    "dispatching",
    "starting_units",
    "watching",
    "restoring",
]
Direction = Literal["candidate", "previous"]
AnyRequest = Annotated[FleetRequest | UnitRequest | PitrRequest, Field(discriminator="kind")]
_REQUEST: TypeAdapter[FleetRequest | UnitRequest | PitrRequest] = TypeAdapter(AnyRequest)
_MAX_JOURNAL_BYTES = 256 * 1024
# Terminal evidence when launchd holds no facts: the recorded boot or login
# domain ended (see cli/release_transition/launcher_macos.py::_current).
_DARWIN_ENDED = frozenset({"boot-changed", "domain-lost"})
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


def read_request(encoded: bytes) -> FleetRequest | UnitRequest | PitrRequest:
    return _REQUEST.validate_json(encoded)


def _kind(launch: dict[str, JsonValue]) -> str:
    """The launch record's required adapter kind; any other value fails validation.

    Every launch is planned by its own adapter, which always writes its kind.
    There is no systemd-shaped default for a record that carries none.
    """
    kind = launch.get("kind")
    if kind in (LINUX, DARWIN):
        return kind
    raise ValueError(f"launch record has an unrecognized adapter kind: {kind!r}")


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
    # macOS only: the home helper and the root its keeper spawned for this start
    # (cli/release_transition/launchd_custody.py::RootCustody). It survives
    # executor attempts; a new direction or helper birth replaces it.
    root: dict[str, JsonValue] | None = None
    # Write-generation fence and issue per direction (authority_evidence.py).
    db_fences: tuple[Fence, ...] = ()
    db_issues: tuple[Issue, ...] = ()
    # Fleet progress: the coordinator's (kind fleet) or a remote unit's (kind unit).
    fleet: FleetProgress | None = None
    unit: UnitProgress | None = None
    error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def coherent_kind(self) -> Self:
        if isinstance(self.request, PitrRequest):
            self._require_pitr_progress()
        else:
            self._require_release_progress(fleet=isinstance(self.request, FleetRequest))
        return self

    def _require_pitr_progress(self) -> None:
        if self.direction is not None or self.pitr is None:
            raise ValueError("PITR requires its own action progress, never release direction")
        if self.fleet is not None or self.unit is not None:
            raise ValueError("PITR carries no fleet progress")
        if self.phase not in {*_PITR_NEXT, "complete"}:
            raise ValueError("PITR cannot enter a release-only phase")

    def _require_release_progress(self, *, fleet: bool) -> None:
        """A fleet (coordinator) or unit operation carries exactly its own progress."""
        progress, other = (self.fleet, self.unit) if fleet else (self.unit, self.fleet)
        if (
            self.pitr is not None
            or self.direction is None
            or progress is None
            or other is not None
            or self.phase not in (FLEET_PHASES if fleet else UNIT_PHASES)
        ):
            raise ValueError(f"a {self.request.kind} operation requires only its own progress")
        if (self.phase == "complete") != (progress.outcome is not None):
            raise ValueError("an outcome is recorded exactly when the operation completes")

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

    @model_validator(mode="after")
    def coherent_launch_kind(self) -> Self:
        if self.launch is not None:
            _kind(self.launch)
        return self

    @model_validator(mode="after")
    def coherent_root(self) -> Self:
        if self.root is None:
            return self
        from cli.release_transition.launchd_custody import RootCustody

        RootCustody.model_validate(self.root)
        if self.pitr is not None or (self.launch is not None and not _darwin(self.launch)):
            raise ValueError("helper root custody belongs only to a macOS release")
        return self

    @model_validator(mode="after")
    def coherent_authority(self) -> Self:
        # Only the fleet coordinator fences and issues; PITR, an abort and a
        # remote unit reuse or receive a generation.
        fleet = self.fleet
        aborted = fleet is not None and any(d.kind == "abort" for d in fleet.decisions)
        direction = self.direction if fleet is not None and not aborted else None
        require_coherent(self.phase, direction, self.db_fences, self.db_issues)
        return self

    def fence(self, direction: Direction) -> Fence | None:
        return next((item for item in self.db_fences if item.direction == direction), None)

    def issue(self, direction: Direction) -> Issue | None:
        return next((item for item in self.db_issues if item.direction == direction), None)

    @property
    def terminal(self) -> bool:
        return self.phase == "complete"

    @property
    def reference(self) -> ReleaseRef:
        """The image this phase runs: an abort restores the unchanged previous one."""
        if isinstance(self.request, PitrRequest):
            return self.request.image
        if self.direction == "previous" or self.phase == "restoring":
            return self.request.previous
        return self.request.candidate

    @property
    def maintenance_at(self) -> datetime:
        for progress in (self.pitr, self.fleet, self.unit):
            if progress is not None:
                return progress.maintenance_at
        raise ValueError("an operation always carries its maintenance hold timestamp")

    def require_configuration(self) -> None:
        if self.pitr is None or self.pitr.seal is None:
            self.request.require_configuration()
            return
        from base.deploy.release.operation import require_configuration

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


def _initial_operation(request: FleetRequest | UnitRequest | PitrRequest) -> Operation:
    """Publish a new intent only while create holds the home operation lock."""
    # Initial admission and every executor share this home lock. A predecessor
    # checked by the caller can change before lock entry.
    reference = request.image if isinstance(request, PitrRequest) else request.previous
    if current_pointer(Path(request.home) / "releases") != reference.selector:
        raise ValueError("prepared predecessor is not the selected release")
    if isinstance(request, PitrRequest):
        from services.pitr.activation.state import record_path

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
        operation = Operation.model_validate({"request": request, **initial_progress(request)})
    _write(operation)
    return operation


def create(request: FleetRequest | UnitRequest | PitrRequest) -> Operation:
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
    """True for the darwin kind, False for linux; any other or missing kind fails fast."""
    return _kind(launch) == DARWIN


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


def _darwin_terminal(terminal: dict[str, JsonValue]) -> bool:
    """Not running, no live owner, and facts matching the terminal's evidence source.

    A boot or login domain that ended leaves launchd no facts: no run count, exit
    code or signal is claimed, and domain loss needs a recorded audit session.
    """
    ended = (terminal["kind"], terminal["state"], terminal["helper"], terminal["executor"])
    if ended != (DARWIN, "not running", None, None):
        return False
    evidence = terminal["evidence"]
    if evidence == "launchd":
        return terminal["runs"] == 1
    facts = (terminal["runs"], terminal["exit_code"], terminal["signal"])
    return (
        evidence in _DARWIN_ENDED
        and facts == (None, None, None)
        and (evidence != "domain-lost" or terminal["asid"] is not None)
    )


def _require_darwin_closure(
    launch: dict[str, JsonValue],
    native: dict[str, JsonValue] | None,
    terminal: dict[str, JsonValue],
) -> None:
    """Terminal launchd facts plus recorded births closed; helper and executor stay distinct."""
    if not _darwin_terminal(terminal):
        raise ValueError("a living or unknown executor cannot be replaced")
    if any(terminal[key] != launch[key] for key in ("label", "domain", "boot_id")):
        raise ValueError("closure belongs to a different native executor")
    closed = (
        None if native is None else {"helper": native["helper"], "executor": native["executor"]}
    )
    if terminal["closed"] != closed or (
        native is not None
        and (terminal["asid"], terminal["pgid"]) != (native["asid"], native["pgid"])
    ):
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
        current = self.operation
        if current.pitr is not None or current.direction is None:
            expected = _PITR_NEXT.get(current.phase)
        else:
            expected = next_phase(_field(current), current.phase, current.direction)
        if expected != phase:
            raise ValueError(f"invalid release transition {current.phase} -> {phase}")
        return self._replace(phase=phase, error=None)

    def record_fleet(self, progress: FleetProgress | UnitProgress) -> Operation:
        """Journal fleet or unit progress before acting on it (or sending it)."""
        current = self.operation.fleet or self.operation.unit
        if current is None or self.operation.terminal or type(progress) is not type(current):
            raise ValueError("fleet progress belongs to an incomplete fleet or unit operation")
        if progress == current:
            return self.operation
        current.require_successor(progress)  # pyright: ignore[reportArgumentType] — same type, checked above
        return self._replace(**{_field(self.operation): progress})

    def complete(self, outcome: Outcome) -> Operation:
        """The last phase transition, with the outcome it completes on."""
        current = self.operation
        progress = current.fleet or current.unit
        if progress is None or current.direction is None:
            raise ValueError("only a fleet or unit operation completes with an outcome")
        if next_phase(_field(current), current.phase, current.direction) != "complete":
            raise ValueError(f"invalid release transition {current.phase} -> complete")
        updated = progress.model_copy(update={"outcome": outcome})
        return self._replace(phase="complete", error=None, **{_field(current): updated})

    def record_fence(self, fence: Fence) -> Operation:
        """Retain a fence's intent before revocation, then its closure receipt.

        A fence belongs to the fencing direction; its generation never changes
        and a closure receipt is never replaced.
        """
        current = self.operation
        if current.phase != "fencing" or fence.direction != current.direction:
            raise ValueError("a write-generation fence is journaled only while fencing")
        prior = current.fence(fence.direction)
        if prior == fence:
            return current
        if prior is None:
            if fence.state != "revoking":
                raise ValueError("a fence records its intent before any closure receipt")
            return self._replace(db_fences=(*current.db_fences, fence))
        if prior.state != "revoking" or prior.generation != fence.generation:
            raise ValueError("a fence cannot change its generation or replace its receipt")
        fences = tuple(fence if item == prior else item for item in current.db_fences)
        return self._replace(db_fences=fences)

    def record_issue(self, issue: Issue) -> Operation:
        """Retain a mint's number before its secret exists, then its admitted generation."""
        current = self.operation
        if current.phase != "authorizing" or issue.direction != current.direction:
            raise ValueError("a write-generation issue is journaled only while authorizing")
        prior = current.issue(issue.direction)
        if prior == issue:
            return current
        if prior is None:
            if issue.state != "minting":
                raise ValueError("an issue records its intent before its authorization")
            return self._replace(db_issues=(*current.db_issues, issue))
        if prior.state != "minting" or (prior.number, prior.selector) != (
            issue.number,
            issue.selector,
        ):
            raise ValueError("an issue cannot change its number or selector, or be replaced")
        issues = tuple(issue if item == prior else item for item in current.db_issues)
        return self._replace(db_issues=issues)

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
        launch = self.operation.launch
        if not self.operation.launch_attempted or launch is None:
            raise ValueError("native birth cannot precede native dispatch")
        if _darwin(launch) and any(
            key not in record or record[key] != launch[key]
            for key in ("kind", "label", "domain", "boot_id")
        ):
            # A stale attempt's executor may reach the lock after a relaunch;
            # its receipt never becomes the current attempt's custody.
            raise ValueError("native receipt belongs to a different executor attempt")
        if self.operation.native is not None:
            if self.operation.native != record and not _same_native_receipt(
                launch, self.operation.native, record
            ):
                raise ValueError("native executor identity changed")
            return self.operation
        return self._replace(native=record)

    def _require_root_start(self) -> None:
        current = self.operation
        if (
            current.pitr is not None
            or current.phase not in {"starting", "restoring"}
            or current.launch is None
            or not current.launch_attempted
            or not _darwin(current.launch)
        ):
            raise ValueError("helper root custody is journaled only while a macOS release starts")

    def root_intent(self, record: dict[str, JsonValue]) -> Operation:
        """Retain helper birth and keeper baseline before the macOS start effect.

        A receipt of this direction is verified, never replaced. An earlier
        intent under the same helper keeps its baseline, so a keeper restart
        before the receipt stays visible; a new helper or direction starts over.
        """
        from cli.release_transition.launchd_custody import RootCustody

        self._require_root_start()
        current = self.operation
        intent = RootCustody.model_validate(record)
        if intent.root is not None or intent.direction != current.direction:
            raise ValueError("root start intent must precede its receipt for this direction")
        if current.root is not None:
            prior = RootCustody.model_validate(current.root)
            if prior.direction == intent.direction and prior.root is not None:
                raise ValueError("a recorded root receipt is verified, never replaced")
            if prior.direction == intent.direction and prior.helper == intent.helper:
                return current
        return self._replace(root=intent.model_dump(mode="json"))

    def root_started(self, record: dict[str, JsonValue]) -> Operation:
        """The start effect's root, under exactly the intent's helper and keeper baseline."""
        from cli.release_transition.launchd_custody import RootCustody

        self._require_root_start()
        current = self.operation
        receipt = RootCustody.model_validate(record)
        if current.root is None or receipt.root is None:
            raise ValueError("root receipt requires its journaled start intent")
        prior = RootCustody.model_validate(current.root)
        if prior.root is not None:
            if prior != receipt:
                raise ValueError("a recorded root receipt cannot change")
            return current
        if prior.model_copy(update={"root": receipt.root}) != receipt:
            raise ValueError("root receipt differs from its intent's direction, helper or baseline")
        return self._replace(root=receipt.model_dump(mode="json"))

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

    def _decide(
        self,
        kind: Literal["abort", "recover"],
        reason: str,
        at: datetime,
        maintenance_at: datetime | None,
    ) -> Operation:
        current = self.operation
        progress = current.fleet or current.unit
        if progress is None:
            raise ValueError("PITR has no automatic release-image rollback")
        if current.terminal or current.direction != "candidate" or progress.decisions:
            raise ValueError("a release decides one abort or one recovery, never reverses it")
        target = decision_target(
            _field(current), current.phase, kind, renewed=maintenance_at is not None
        )
        decision = Decision(kind=kind, phase=current.phase, reason=reason[:2048], at=at)
        updated = progress.decided(decision, maintenance_at)
        direction = "previous" if kind == "recover" else "candidate"
        return self._replace(
            phase=target, direction=direction, error=reason[:2048], **{_field(current): updated}
        )

    def recover(
        self, reason: str, *, at: datetime, maintenance_at: datetime | None = None
    ) -> Operation:
        """Choose the captured predecessor once, after the fence.

        Before admission reopened the same maintenance hold stops the candidate;
        a recovery from `watching` (or a resumed unit) drains again under the
        new `maintenance_at`.
        """
        return self._decide("recover", reason, at, maintenance_at)

    def abort(self, reason: str, *, at: datetime) -> Operation:
        """Before the fence: restore the unchanged previous image on generation n."""
        return self._decide("abort", reason, at, None)

    def fail(self, detail: str) -> Operation:
        """Retain the uncertain phase; an exception is never a closure receipt."""
        if self.operation.terminal:
            raise ValueError("a completed release operation cannot become failed")
        return self._replace(error=detail)


def _field(operation: Operation) -> Literal["fleet", "unit"]:
    return "fleet" if operation.fleet is not None else "unit"


@contextmanager
def exclusive(path: Path) -> Generator[Journal]:
    operation = read_operation(path)
    home = Path(operation.request.home)
    with file_lock(_lock(home), timeout_s=5):
        active = Path(regular_bytes(home / "updates" / "active").decode().strip())
        if active != path:
            raise ValueError("this is not the home's active release operation")
        yield Journal(read_operation(path))
