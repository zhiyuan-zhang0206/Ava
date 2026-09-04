"""Sealed normal candidate planning with activation deliberately disabled.

The planner binds the existing per-unit updater, bootstrap evidence, publication
plan and service identities. Execution refuses before updater ownership or
effects until checked phase recovery and exact spawn receipts are implemented.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import psycopg
from pydantic import Field

from cli.commands._release_selector import (
    pending_transaction,
    read_selector,
    selector_bytes,
)
from cli.commands._release_services import (
    PreparedService,
    prepare_normal_services,
)
from cli.commands._update_bootstrap import (
    BootstrapHopRequest,
    PreparedBootstrapHop,
    _private_reference,
    probe_bootstrap,
)
from services.agent_ops.bootstrap import (
    ObserverProjection,
    PreparedObservation,
    read_prepared_context,
    validate_operation,
)
from shared import updater_handoff
from shared.managed_writer_barrier import EvidenceModel, lock_rollout
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.managed_writer_publication import CandidateUnitPlan, PublishedUnit, _locked_publication
from shared.runtime_release import ReleaseRejectedError
from shared.session_record import SessionRecord
from shared.updater_recovery import BootstrapRecoveryJournal
from shared.verified_file import regular_bytes


class NormalReleaseRequest(EvidenceModel):
    context_path: str
    unit: PublishedUnit
    previous_selector: str | None = Field(max_length=65536)
    predecessor: ExpectedProcess


@dataclass(frozen=True)
class PreparedNormalRelease:
    request_path: Path
    request: NormalReleaseRequest
    context: PreparedObservation
    projection: ObserverProjection
    services: tuple[PreparedService, ...]
    bootstrap: SessionRecord
    resume_generation: str


def require_checked_normal_activation() -> Never:
    """Refuse effects until exact spawn receipts and stage recovery are implemented."""
    raise ReleaseRejectedError(
        "normal activation requires checked crash recovery and exact spawn receipts"
    )


def _preflight_pending_plan(plan: PreparedNormalRelease) -> None:
    context = plan.context
    previous = plan.request.previous_selector
    expected = CandidateUnitPlan(
        unit=plan.request.unit,
        services=tuple(
            sorted((item.identity for item in plan.services), key=lambda item: item.session)
        ),
        previous_selector_digest=None
        if previous is None
        else hashlib.sha256(previous.encode()).hexdigest(),
        selector_digest=hashlib.sha256(selector_bytes(plan.request.unit)).hexdigest(),
    )
    remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds())
    if remaining < 2:
        raise ReleaseRejectedError("normal plan has no pre-stop connection budget")
    with (
        psycopg.connect(
            plan.projection.db_url.get_secret_value(),
            autocommit=True,
            connect_timeout=min(5, remaining),
        ) as conn,
        pending_transaction(conn, context),
    ):
        lock_rollout(conn, context.operation)
        pending = _locked_publication(conn).pending
        if (
            pending is None
            or pending.operation != context.operation
            or pending.challenge != context.challenge.challenge
            or pending.normal_start_plan is None
            or expected not in pending.normal_start_plan.units
        ):
            raise ReleaseRejectedError(
                "complete normal command/selector plan is absent before stop"
            )
        lock_rollout(conn, context.operation)


def prepare_after_bootstrap(hop: PreparedBootstrapHop) -> PreparedNormalRelease:
    """Prepare the optional normal continuation BEFORE the same updater stops A."""
    reference = hop.request.normal_release_path
    if reference is None:
        raise ReleaseRejectedError("normal continuation reference is absent")
    path = _private_reference(reference, Path(hop.candidate.expected.home))
    request = NormalReleaseRequest.model_validate_json(regular_bytes(path))
    context = read_prepared_context(
        _private_reference(request.context_path, Path(request.unit.home))
    )
    if context != hop.candidate or request.predecessor != hop.request.predecessor:
        raise ReleaseRejectedError("normal continuation belongs to a different bootstrap operation")
    services = prepare_normal_services(request.unit, context.schema_digest)
    if not any(service.identity.session == "ava-ops" for service in services):
        raise ReleaseRejectedError("normal continuation lacks same-endpoint ops")
    previous = request.previous_selector.encode() if request.previous_selector is not None else None
    if read_selector(Path(request.unit.home)) != previous:
        raise ReleaseRejectedError("normal selector predecessor differs before bootstrap stop")
    record = SessionRecord(
        **json.loads(regular_bytes(Path(request.unit.home) / "run/sessions/ava-ops.json"))
    )
    prepared = PreparedNormalRelease(path, request, context, hop.projection, services, record, "")
    _preflight_pending_plan(prepared)
    return prepared


def _candidate_ready_recovery(generation: str) -> BootstrapRecoveryJournal:
    try:
        retained = updater_handoff.read_bootstrap_recovery()
        journal = (
            BootstrapRecoveryJournal.model_validate_json(json.dumps(retained["journal"]))
            if retained is not None
            else None
        )
    except (KeyError, TypeError, ValueError, updater_handoff.BootstrapRecoveryInvalidError) as exc:
        raise ReleaseRejectedError("normal continuation recovery is malformed") from exc
    if (
        retained is None
        or retained["generation"] != generation
        or journal is None
        or journal.stage != "candidate_ready"
        or not journal.normal_release_planned
        or journal.normal_release is not None
    ):
        raise ReleaseRejectedError(
            "normal continuation requires the actual candidate-ready handoff"
        )
    return journal


def continue_after_bootstrap(
    hop: PreparedBootstrapHop, plan: PreparedNormalRelease, generation: str
) -> Never:
    """Validate retained identity, then refuse the disabled activation route."""
    journal = _candidate_ready_recovery(generation)
    if (
        journal.request != str(hop.request_path)
        or journal.request_digest != hashlib.sha256(regular_bytes(hop.request_path)).hexdigest()
        or journal.inventory_digest
        != hashlib.sha256(regular_bytes(Path(hop.request.inventory_receipt))).hexdigest()
        or journal.candidate_context_digest
        != hashlib.sha256(regular_bytes(Path(hop.request.candidate_context))).hexdigest()
        or journal.recovery_context_digest
        != hashlib.sha256(regular_bytes(Path(hop.request.recovery_context))).hexdigest()
        or hop.request.normal_release_path != str(plan.request_path)
    ):
        raise ReleaseRejectedError("normal continuation differs from its retained bootstrap")
    require_checked_normal_activation()


def prepare_normal_release(path: Path) -> PreparedNormalRelease:
    """Validate all local support/identity inputs while the old observer serves."""
    request = NormalReleaseRequest.model_validate_json(regular_bytes(path))
    home = Path(request.unit.home)
    _private_reference(str(path), home)
    context = read_prepared_context(_private_reference(request.context_path, home))
    if (
        context.expected.machine,
        context.expected.home,
        context.expected.artifact_digest,
        context.expected.manifest_digest,
    ) != (
        request.unit.machine,
        request.unit.home,
        request.unit.artifact_digest,
        request.unit.manifest_digest,
    ):
        raise ReleaseRejectedError("normal request differs from the prepared observation")
    if observe_process(request.predecessor) != "exited":
        raise ReleaseRejectedError("old orchestrator has not positively relinquished this unit")
    handoff = updater_handoff.read()
    if (
        handoff.status != "running"
        or handoff.generation is None
        or handoff.owner_pid != request.predecessor.pid
        or handoff.owner_create_time != request.predecessor.create_time
        or updater_handoff.owner_is_live(handoff)
    ):
        raise ReleaseRejectedError("existing handoff does not identify the exited orchestrator")
    journal = _candidate_ready_recovery(handoff.generation)
    bootstrap_path = _private_reference(journal.request, home)
    bootstrap_request = BootstrapHopRequest.model_validate_json(regular_bytes(bootstrap_path))
    if (
        journal.request_digest != hashlib.sha256(regular_bytes(bootstrap_path)).hexdigest()
        or bootstrap_request.normal_release_path != str(path)
        or bootstrap_request.predecessor != request.predecessor
        or bootstrap_request.candidate_context != request.context_path
        or journal.inventory_digest
        != hashlib.sha256(
            regular_bytes(_private_reference(bootstrap_request.inventory_receipt, home))
        ).hexdigest()
        or journal.candidate_context_digest
        != hashlib.sha256(regular_bytes(Path(request.context_path))).hexdigest()
        or journal.recovery_context_digest
        != hashlib.sha256(
            regular_bytes(_private_reference(bootstrap_request.recovery_context, home))
        ).hexdigest()
    ):
        raise ReleaseRejectedError("normal continuation has no exact completed bootstrap handoff")
    services = prepare_normal_services(request.unit, context.schema_digest)
    if not any(service.identity.session == "ava-ops" for service in services):
        raise ReleaseRejectedError("unit has no normal same-endpoint ops service")
    previous = request.previous_selector.encode() if request.previous_selector is not None else None
    if read_selector(home) != previous:
        raise ReleaseRejectedError("normal selector predecessor differs before maintenance")
    projection = ObserverProjection.from_environment()
    validate_operation(context, projection)
    probe_bootstrap(context, projection)
    bootstrap = SessionRecord(**json.loads(regular_bytes(home / "run/sessions/ava-ops.json")))
    # probe_bootstrap checks real command, actual self-report and native ownership;
    # retain that exact record so no later lookup can signal a replacement.
    if (
        observe_process(
            ExpectedProcess(
                pid=bootstrap.pid, create_time=bootstrap.create_time, starttime=bootstrap.starttime
            )
        )
        != "alive"
    ):
        raise ReleaseRejectedError("verified bootstrap process disappeared")
    prepared = PreparedNormalRelease(
        path, request, context, projection, services, bootstrap, handoff.generation
    )
    _preflight_pending_plan(prepared)
    return prepared


def execute_normal_release(_plan: PreparedNormalRelease, _generation: str) -> Never:
    """Defensive fence for callers that already hold a prepared plan."""
    require_checked_normal_activation()


def run_normal_release(path: Path) -> Never:
    prepare_normal_release(path)
    require_checked_normal_activation()
