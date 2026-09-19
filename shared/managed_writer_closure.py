"""Platform-independent derivation of positive managed-writer closure.

The observer reports facts only; the positive ``old_writers_absent_relaunchers_fenced``
literal is produced where those facts meet the hop ledger. This module is that
derivation and assembly layer: pure functions over typed facts and journaled
launcher terminals, no filesystem, network or database access.

Only exact facts can produce a closure: every prepared launcher must be fenced
by its journaled terminal (removed = the definition is positively absent;
rebound = the definition now carries exactly the terminal's new digest), every
prepared old-writer process must be observed exited, every prepared session
absent, and the echoed challenge and observation window must match the
operation. Anything unknown, missing, duplicated or drifted assembles nothing —
``None`` is the only refusal value: not assembling is the fail-closed default.

The storage gate is unchanged and remains the only adoption authority
(``shared.managed_writer_publication.adopt_pending_collection`` revalidates the
whole collection under the locked rollout). The collector side (network
gathering, the platform final re-read after the candidate is ready) and the
platform hop channels connect this layer later; this seat is inert until then
(no production caller).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.managed_writer_barrier import (
    Digest,
    EvidenceModel,
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_observation import (
    ExpectedUnitWriters,
    ProcessVerdict,
    SessionVerdict,
)
from shared.native_job_observation import LauncherObservation


class LauncherTerminal(EvidenceModel):
    """One prepared launcher's journaled terminal record (the hop ledger).

    ``label`` is the prepared launcher's name (``ExpectedLauncher.name``: the
    launchd label, or the crontab definition digest). ``removed`` requires the
    launcher definition to be positively absent; ``rebound`` requires it to
    have been rewritten to exactly ``new_digest``.
    """

    label: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    kind: Literal["removed", "rebound"]
    new_digest: Digest | None = None

    @model_validator(mode="after")
    def coherent_digest(self) -> Self:
        if self.kind == "rebound" and self.new_digest is None:
            raise ValueError("rebound terminal requires its new definition digest")
        if self.kind == "removed" and self.new_digest is not None:
            raise ValueError("removed terminal carries no new digest")
        return self


def launcher_fenced(facts: LauncherObservation, terminal: LauncherTerminal) -> bool:
    """Whether one launcher's facts meet its journaled terminal.

    Both D(l) (the definition is gone, or rebound to the terminal's exact
    bytes) and L(l) (the scheduler positively does not have it loaded) must
    hold. Crontab observations never set ``loaded``, so they fence nothing
    here — fail-closed until the Linux collector rule is designed.
    """
    if facts.loaded is not False:
        return False
    match terminal.kind:
        case "removed":
            return facts.definition == "absent" and facts.current_digest is None
        case "rebound":
            return (
                facts.definition == "mismatch"
                and facts.current_digest is not None
                and facts.current_digest == terminal.new_digest
            )
        case _:
            return False


def _canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def assemble_unit_closure(
    expected: ExpectedUnitWriters,
    *,
    operation: RolloutIdentity,
    operation_challenge: UUID,
    echoed_challenge: UUID,
    boot_id: UUID,
    observer_instance: UUID,
    observed_unit: ManagedUnit,
    observed_at: AwareDatetime,
    valid_until: AwareDatetime,
    processes: tuple[ProcessVerdict, ...],
    sessions: tuple[SessionVerdict, ...],
    launchers: tuple[LauncherObservation, ...],
    terminals: tuple[LauncherTerminal, ...],
) -> ManagedUnitClosure | None:
    """Assemble one unit's closure from its final facts and the hop ledger.

    The caller must have performed the platform final re-read (definition
    bytes, membership, old writers) after the candidate was ready and passes
    those exact final facts here; this function judges them only. Facts are
    positional against ``expected`` (the observer reports in prepared order).
    """
    if observed_unit != expected.unit():
        return None
    if echoed_challenge != operation_challenge:
        return None
    if not operation.acquired_at <= observed_at < valid_until:
        return None
    if len(processes) != len(expected.processes) or any(
        verdict != "exited" for verdict in processes
    ):
        return None
    if len(sessions) != len(expected.sessions) or any(verdict != "absent" for verdict in sessions):
        return None
    if len(launchers) != len(expected.launchers):
        return None
    by_label = {terminal.label: terminal for terminal in terminals}
    if len(by_label) != len(terminals) or set(by_label) != {
        item.name for item in expected.launchers
    }:
        return None
    for entry, facts in zip(expected.launchers, launchers, strict=True):
        if not launcher_fenced(facts, by_label[entry.name]):
            return None
    payload = {
        "operation": operation.model_dump(mode="json"),
        "unit": observed_unit.model_dump(mode="json"),
        "boot_id": str(boot_id),
        "observer_instance": str(observer_instance),
        "challenge": str(echoed_challenge),
        "observed_at": observed_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "processes": list(processes),
        "sessions": list(sessions),
        "launchers": [item.model_dump(mode="json") for item in launchers],
        "terminals": [
            item.model_dump(mode="json") for item in sorted(terminals, key=lambda item: item.label)
        ],
    }
    return ManagedUnitClosure(
        unit=observed_unit,
        boot_id=boot_id,
        observer_instance=observer_instance,
        observation_digest=hashlib.sha256(_canonical(payload).encode()).hexdigest(),
        outcome="old_writers_absent_relaunchers_fenced",
    )


def assemble_collection(
    *,
    operation: RolloutIdentity,
    candidate_digest: Digest,
    challenge: UUID,
    collected_at: AwareDatetime,
    valid_until: AwareDatetime,
    expected_units: tuple[ManagedUnit, ...],
    closures: tuple[ManagedUnitClosure, ...],
) -> ManagedWriterCollection | None:
    """Assemble the single all-unit collection; a missing or extra unit refuses.

    ``expected_units`` is the prepared unit tuple; ``closures`` must cover
    exactly those units. Storage still revalidates everything at adoption;
    this is the assembly guard, not the gate.
    """
    if not expected_units or not closures:
        return None
    expected_keys = tuple((unit.machine, unit.home) for unit in expected_units)
    if expected_keys != tuple(sorted(set(expected_keys))):
        return None
    observed_keys = tuple((entry.unit.machine, entry.unit.home) for entry in closures)
    if observed_keys != expected_keys:
        return None
    if not (collected_at >= operation.acquired_at and valid_until > collected_at):
        return None
    return ManagedWriterCollection(
        operation=operation,
        candidate_digest=candidate_digest,
        challenge=challenge,
        collected_at=collected_at,
        valid_until=valid_until,
        units=closures,
    )
