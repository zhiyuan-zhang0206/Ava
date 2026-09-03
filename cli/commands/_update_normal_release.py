"""Normal candidate continuation of the existing per-unit updater.

The detached updater keeps its existing local flock and handoff journal while
waiting on the one deployment record. Bootstrap remains read-only. A lost lease
leaves recovery evidence, not permission to restore a selector or start a process.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psutil
import psycopg
from pydantic import Field

from cli.commands._release_selector import (
    pending_transaction,
    read_selector,
    select_pending_release,
    selector_bytes,
)
from cli.commands._release_services import (
    PreparedService,
    prepare_normal_services,
    start_normal_service,
)
from cli.commands._update_bootstrap import PreparedBootstrapHop, _private_reference, probe_bootstrap
from services.agent_ops.bootstrap import (
    ObserverProjection,
    PreparedObservation,
    read_prepared_context,
    validate_operation,
)
from shared import updater_handoff
from shared.host_deploy_state import release_updater_lock, try_acquire_updater_lock
from shared.managed_writer_activation import (
    NormalServiceReadback,
    SelectorReadback,
    UnitActivationReadback,
    pending_stage,
    require_pending_candidate_start,
)
from shared.managed_writer_barrier import EvidenceModel, lock_rollout
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.managed_writer_publication import CandidateUnitPlan, PublishedUnit, _locked_publication
from shared.platform import file_lock
from shared.runtime_release import ReleaseRejectedError
from shared.session_backend import get_backend
from shared.session_record import SessionRecord
from shared.verified_file import regular_bytes


class NormalReleaseRequest(EvidenceModel):
    context_path: str
    unit: PublishedUnit
    previous_selector: str | None = Field(max_length=65536)
    predecessor: ExpectedProcess


class NormalReleaseJournal(EvidenceModel):
    request_path: str
    operation_context: PreparedObservation
    unit: PublishedUnit
    previous_selector: str | None
    stage: Literal["waiting", "selected", "bootstrap_stopped", "starting", "observed", "committed"]
    starting_session: str | None = None
    readback: UnitActivationReadback | None = None


@dataclass(frozen=True)
class PreparedNormalRelease:
    request_path: Path
    request: NormalReleaseRequest
    context: PreparedObservation
    projection: ObserverProjection
    services: tuple[PreparedService, ...]
    bootstrap: SessionRecord
    resume_generation: str


def _preflight_pending_plan(plan: PreparedNormalRelease) -> None:
    context = plan.context
    previous = plan.request.previous_selector
    expected = CandidateUnitPlan(
        unit=plan.request.unit,
        services=tuple(item.identity for item in plan.services),
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


def continue_after_bootstrap(
    hop: PreparedBootstrapHop, plan: PreparedNormalRelease, generation: str
) -> int:
    """Same process/flock/operation: no second dispatcher or bootstrap write route."""
    retained = json.loads(regular_bytes(updater_handoff.state_path()))
    if (
        retained["generation"] != generation
        or retained["bootstrap_hop"]["stage"] != "candidate_ready"
    ):
        raise ReleaseRejectedError(
            "normal continuation requires the actual candidate-ready handoff"
        )
    probe_bootstrap(plan.context, plan.projection, verified_image=hop.image)
    path = Path(plan.request.unit.home) / "run/sessions/ava-ops.json"
    record = SessionRecord(**json.loads(regular_bytes(path)))
    return execute_normal_release(
        replace(plan, bootstrap=record, resume_generation=generation), generation
    )


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
    retained = json.loads(regular_bytes(updater_handoff.state_path()))
    if "normal_release" in retained:
        raise ReleaseRejectedError("unfinished normal release requires explicit checked recovery")
    bootstrap_journal = retained["bootstrap_hop"]
    if (
        bootstrap_journal["stage"] != "candidate_ready"
        or bootstrap_journal["candidate_context_digest"]
        != hashlib.sha256(regular_bytes(Path(request.context_path))).hexdigest()
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


def _journal(generation: str, value: NormalReleaseJournal) -> None:
    with file_lock(updater_handoff.lock_path(), timeout_s=5):
        current = updater_handoff.read()
        if (
            current.generation != generation
            or current.status != "running"
            or current.owner_pid != os.getpid()
            or current.owner_create_time != psutil.Process().create_time()
        ):
            raise ReleaseRejectedError("normal updater no longer owns its local handoff")
        path = updater_handoff.state_path()
        payload = json.loads(regular_bytes(path))
        payload["normal_release"] = value.model_dump(mode="json")
        updater_handoff._write_atomic(path, payload)


def _wait_stage(conn: psycopg.Connection, plan: PreparedNormalRelease, expected: str) -> None:
    context = plan.context
    while datetime.now(UTC) < context.challenge.valid_until:
        with pending_transaction(conn, context):
            stage = pending_stage(conn, context.operation, context.challenge.challenge)
        if stage == expected:
            return
        if stage == "committed":
            raise ReleaseRejectedError("unexpected publication transition")
        time.sleep(
            min(0.2, max(0, (context.challenge.valid_until - datetime.now(UTC)).total_seconds()))
        )
    raise ReleaseRejectedError("normal continuation exhausted its original challenge")


def _stop_bootstrap(
    conn: psycopg.Connection, plan: PreparedNormalRelease, selector: SelectorReadback
) -> None:
    context = plan.context
    ops = next(service for service in plan.services if service.identity.session == "ava-ops")
    with pending_transaction(conn, context):
        require_pending_candidate_start(
            conn, context.operation, context.challenge.challenge, selector, ops.identity
        )
    validate_operation(context, plan.projection)
    probe_bootstrap(context, plan.projection)
    path = Path(plan.request.unit.home) / "run/sessions/ava-ops.json"
    if SessionRecord(**json.loads(regular_bytes(path))) != plan.bootstrap:
        raise ReleaseRejectedError("bootstrap session changed before normal transition")
    with pending_transaction(conn, context):
        require_pending_candidate_start(
            conn, context.operation, context.challenge.challenge, selector, ops.identity
        )
    if not get_backend().graceful_signal("ava-ops", expected=plan.bootstrap):
        raise ReleaseRejectedError("exact bootstrap stop failed")
    process = ExpectedProcess(
        pid=plan.bootstrap.pid,
        create_time=plan.bootstrap.create_time,
        starttime=plan.bootstrap.starttime,
    )
    while datetime.now(UTC) < context.challenge.valid_until:
        validate_operation(context, plan.projection)
        verdict = observe_process(process)
        if verdict == "exited":
            return
        if verdict != "alive":
            raise ReleaseRejectedError("bootstrap exit identity is unknown")
        time.sleep(0.05)
    raise ReleaseRejectedError("bootstrap did not stop within the original challenge")


def execute_normal_release(plan: PreparedNormalRelease, generation: str) -> int:
    """Actual selector/start path, only after adopted barrier and real migration."""
    context = plan.context
    state = NormalReleaseJournal(
        request_path=str(plan.request_path),
        operation_context=context,
        unit=plan.request.unit,
        previous_selector=plan.request.previous_selector,
        stage="waiting",
    )
    _journal(generation, state)
    remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds())
    if remaining < 2:
        raise ReleaseRejectedError("normal continuation has no connection budget")
    with psycopg.connect(
        plan.projection.db_url.get_secret_value(),
        autocommit=True,
        connect_timeout=min(5, remaining),
    ) as conn:
        _wait_stage(conn, plan, "selector_allowed")
        previous = state.previous_selector.encode() if state.previous_selector is not None else None
        selector = select_pending_release(conn, context, plan.request.unit, previous)
        state = state.model_copy(update={"stage": "selected"})
        _journal(generation, state)
        _stop_bootstrap(conn, plan, selector)
        state = state.model_copy(update={"stage": "bootstrap_stopped"})
        _journal(generation, state)
        results: list[NormalServiceReadback] = []
        # Never rediscover mutable gates after stop: preparation pinned this order.
        for service in plan.services:
            state = state.model_copy(
                update={"stage": "starting", "starting_session": service.identity.session}
            )
            _journal(generation, state)
            results.append(start_normal_service(conn, context, selector, service))
        readback = UnitActivationReadback(
            selector=selector,
            services=tuple(sorted(results, key=lambda item: item.service.session)),
        )
        state = state.model_copy(
            update={"stage": "observed", "starting_session": None, "readback": readback}
        )
        _journal(generation, state)
        # The all-unit coordinator consumes this same pending field. No bootstrap
        # mutation RPC, new listener, or unauthenticated callback is introduced.
        from shared.managed_writer_activation import record_pending_unit_readback

        with pending_transaction(conn, context):
            record_pending_unit_readback(
                conn, context.operation, context.challenge.challenge, readback
            )
        _wait_stage(conn, plan, "committed")
        _journal(generation, state.model_copy(update={"stage": "committed"}))
    return 0


def run_normal_release(path: Path) -> int:
    plan = prepare_normal_release(path)
    if not try_acquire_updater_lock():
        raise ReleaseRejectedError("another updater holds this unit")
    generation: str | None = None
    claimed = False
    try:
        expected = f"direct-updater:pid{os.getpid()}"
        generation = plan.resume_generation
        if not updater_handoff.resume_bootstrap(generation, expected_session=expected):
            raise ReleaseRejectedError("normal updater could not claim its existing handoff")
        claimed = True
        return execute_normal_release(plan, generation)
    finally:
        if generation is not None and claimed:
            updater_handoff.clear(generation)
        release_updater_lock()
