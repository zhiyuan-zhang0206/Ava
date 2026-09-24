"""Read-only unit pairing and predecessor ownership for the restricted hop."""

import hashlib
from pathlib import Path

from cli.commands._release_inventory import _regular_bytes
from services.agent_ops.bootstrap import PreparedObservation
from shared import updater_handoff
from shared.managed_writer_observation import ExpectedProcess, ExpectedSession, ExpectedUnitWriters
from shared.runtime_release import ReleaseRejectedError
from shared.updater_recovery import BootstrapRecoveryJournal


def verify_pair(candidate: PreparedObservation, recovery: PreparedObservation) -> None:
    if (
        recovery.operation != candidate.operation
        or recovery.expected.machine != candidate.expected.machine
        or recovery.expected.home != candidate.expected.home
        or recovery.expected.artifact_digest == candidate.expected.artifact_digest
    ):
        raise ReleaseRejectedError("bootstrap A/B images do not share the exact operation/unit")


def retained_handoff(
    process: ExpectedProcess,
    journal: BootstrapRecoveryJournal | None,
) -> updater_handoff.UpdaterHandoffSnapshot | None:
    if journal is not None:
        return None
    predecessor = updater_handoff.read()
    if (
        predecessor.status != "running"
        or predecessor.owner_pid != process.pid
        or predecessor.owner_create_time != process.create_time
        or updater_handoff.owner_is_live(predecessor)
    ):
        raise ReleaseRejectedError("existing updater handoff does not prove predecessor exit")
    return predecessor


def single_session(
    expected: ExpectedUnitWriters, candidate: PreparedObservation
) -> ExpectedSession:
    if expected != candidate.expected or len(expected.sessions) != 1:
        raise ReleaseRejectedError("restricted hop cannot stop ordinary or additional sessions")
    session = expected.sessions[0]
    if session.name != "ava-ops" or expected.processes != (session.process,):
        raise ReleaseRejectedError("restricted hop requires one exact existing ops session")
    return session


def verify_retained_contexts(
    candidate_path: Path, recovery_path: Path, journal: BootstrapRecoveryJournal | None
) -> None:
    if journal is not None and (
        journal.candidate_context_digest
        != hashlib.sha256(_regular_bytes(candidate_path)).hexdigest()
        or journal.recovery_context_digest
        != hashlib.sha256(_regular_bytes(recovery_path)).hexdigest()
    ):
        raise ReleaseRejectedError("bootstrap recovery contexts changed")
