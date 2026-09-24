"""Sealed normal candidate planning with the checked activation chain.

The planner binds the existing per-unit updater, bootstrap evidence, publication
plan and service identities. ``execute_normal_release`` is the activation entry
and drives the checked chain (``_drive_checked_normal_release``): one stage
machine over waiting -> selected -> bootstrap_stopped -> starting -> observed
-> committed. Every entry — fresh continuation, resumed continuation, standalone
recovery — runs the same reconciliation: each stage first adjudicates its
retained evidence, skips what is already proved, and performs only the missing
effect under fresh authority (idempotent re-entry; design #4117 §5). Service
starts go through the gated spawn (per-session gate + pre-exec birth receipt)
and are cross-checked against their exact session record; a spawn whose outcome
cannot be proven refuses and retains its evidence. The flip that removed the
activation fence (design §7.1) declared ``CHECKED_ACTIVATION_READY`` at module
level; the managed-writer mode gate consumes that declaration fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
    adopt_birth_record,
    await_normal_service_ready,
    normal_spawn_command,
    prepare_normal_services,
    start_normal_service,
)
from cli.commands._update_bootstrap import (
    PreparedBootstrapHop,
    _private_reference,
)
from services.agent_ops.bootstrap import (
    ObserverProjection,
    PreparedObservation,
    read_prepared_context,
    validate_operation,
)
from shared import spawn_receipt, updater_handoff
from shared.config import settings
from shared.managed_writer_activation import (
    UnitActivationReadback,
    read_pending_unit_readbacks,
    record_pending_migration,
    record_pending_unit_readback,
)
from shared.managed_writer_barrier import EvidenceModel, lock_rollout
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalServiceReadback,
    PublishedUnit,
    SelectorReadback,
    _locked_publication,
)
from shared.runtime_release import ReleaseRejectedError
from shared.session_backend import get_backend
from shared.session_record import SessionRecord
from shared.updater_recovery import (
    BootstrapRecoveryJournal,
    NormalReleaseRecoveryJournal,
    PreparedObservationRecovery,
    SpawnAttempt,
    SpawnVerdict,
)
from shared.verified_file import regular_bytes

# The flip declaration (design §7.1, task #4117 S5): lands in the same change
# that removes the activation fence. ``cli.commands._managed_writer_mode`` reads
# this attribute and enters only on an exactly-True value — absent or non-True
# refuses fail-closed; revert = the same diff reversed.
CHECKED_ACTIVATION_READY = True


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


def _prepared_recovery(context: PreparedObservation) -> PreparedObservationRecovery:
    """The journal's identity face for one prepared observation."""
    return PreparedObservationRecovery(
        expected=context.expected,
        operation=context.operation,
        challenge=context.challenge,
        schema_digest=context.schema_digest,
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
    # prepare_threshold=None: never prepare statements on the pooled front door.
    with (
        psycopg.connect(
            plan.projection.db_url.get_secret_value(),
            autocommit=True,
            prepare_threshold=None,
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
    """The candidate-ready bootstrap journal for this exact generation.

    A retained nested normal journal is not a refusal: the checked chain reads
    it and resumes from its recorded stage (design §5.0 — both recovery entries
    share the same stage machine and judgment). A different generation or a
    bootstrap that is not a planned candidate-ready handoff refuses.
    """
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
    ):
        raise ReleaseRejectedError(
            "normal continuation requires the actual candidate-ready handoff"
        )
    return journal


def continue_after_bootstrap(
    hop: PreparedBootstrapHop, plan: PreparedNormalRelease, generation: str
) -> int:
    """Validate retained identity, then enter the checked activation entry.

    ``prepare_after_bootstrap`` ran before the hop stopped A, so its retained
    record predates B; the current ``ava-ops`` record — B, the process the hop
    actually left serving — is re-read here and bound into the plan, so the
    stop stage compares against that exact process and no later lookup can
    signal a replacement.
    """
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
    bootstrap = _read_ops_record(Path(plan.request.unit.home))
    return execute_normal_release(replace(plan, bootstrap=bootstrap), generation)


def _read_ops_record(home: Path) -> SessionRecord:
    """Strict read of the unit's current ``ava-ops`` session record.

    Absence or damage refuses: the stop stage's exact-identity compare must
    never proceed on an unreadable record (damaged is never absent).
    """
    try:
        body = regular_bytes(home / "run" / "sessions" / "ava-ops.json")
    except FileNotFoundError as exc:
        raise ReleaseRejectedError("no ava-ops session record exists for this unit") from exc
    except OSError as exc:
        raise ReleaseRejectedError("ava-ops session record cannot be read") from exc
    try:
        return SessionRecord(**json.loads(body))
    except (TypeError, ValueError) as exc:
        raise ReleaseRejectedError("ava-ops session record is malformed") from exc


def _stop_bootstrap_checked(plan: PreparedNormalRelease) -> None:
    """Stop the restricted candidate observer by its retained exact identity.

    The record is re-read strictly and must equal the retained record (B is
    the only writer of that session in this window). A process already gone —
    ``exited``, or a reused pid whose birth differs (A7) — is already stopped;
    anything unobservable refuses. The signal is only sent to a proven-alive
    exact identity, after a fresh operation validation.
    """
    record = _read_ops_record(Path(plan.request.unit.home))
    if record != plan.bootstrap:
        raise ReleaseRejectedError("normal stop no longer identifies the retained bootstrap")
    process = ExpectedProcess(
        pid=record.pid, create_time=record.create_time, starttime=record.starttime
    )
    verdict = observe_process(process)
    if verdict in {"exited", "identity_mismatch"}:
        return
    if verdict != "alive":
        raise ReleaseRejectedError("normal stop has an unknown bootstrap identity")
    validate_operation(plan.context, plan.projection)
    if not get_backend().graceful_signal("ava-ops", expected=record):
        raise ReleaseRejectedError("exact bootstrap stop was refused")
    _wait_bootstrap_stopped(plan, process)


def _wait_bootstrap_stopped(plan: PreparedNormalRelease, process: ExpectedProcess) -> None:
    # Leave half the remaining challenge for the following service work.
    remaining = (plan.context.challenge.valid_until - datetime.now(UTC)).total_seconds()
    deadline = time.monotonic() + min(5, max(0.0, remaining / 2))
    while time.monotonic() < deadline:
        verdict = observe_process(process)
        if verdict in {"exited", "identity_mismatch"}:
            return
        if verdict != "alive":
            raise ReleaseRejectedError("bootstrap stop has an unknown identity")
        time.sleep(0.05)
    raise ReleaseRejectedError("bootstrap did not stop within its recovery budget")


def _write_normal_journal(
    generation: str, journal: NormalReleaseRecoveryJournal
) -> NormalReleaseRecoveryJournal:
    """One journal CAS through the ownership- and transition-checked writer."""
    try:
        updater_handoff.write_normal_release_recovery(generation, journal.model_dump(mode="json"))
    except updater_handoff.BootstrapRecoveryInvalidError as exc:
        raise ReleaseRejectedError(f"normal recovery journal refused this write: {exc}") from exc
    return journal


def _retained_normal_journal(
    plan: PreparedNormalRelease, generation: str
) -> NormalReleaseRecoveryJournal | None:
    """The retained nested journal for this exact plan, or None when fresh.

    The chain is re-entrant from any recorded stage; a journal whose identity
    face differs from the prepared plan refuses (evidence is never repaired to
    fit a plan).
    """
    bootstrap = _candidate_ready_recovery(generation)
    journal = bootstrap.normal_release
    if journal is None:
        return None
    if (
        journal.request_path != str(plan.request_path)
        or journal.operation_context != _prepared_recovery(plan.context)
        or journal.unit != plan.request.unit
        or journal.previous_selector != plan.request.previous_selector
    ):
        raise ReleaseRejectedError("retained normal recovery differs from its prepared plan")
    return journal


def _attempt_for(
    plan: PreparedNormalRelease, generation: str, prepared: PreparedService
) -> SpawnAttempt:
    """Mint one exact attempt for a service about to be spawned."""
    home = Path(plan.request.unit.home)
    session = prepared.identity.session
    nonce = uuid4()
    return SpawnAttempt(
        nonce=nonce,
        session=session,
        cmd_digest=hashlib.sha256(normal_spawn_command(prepared).encode()).hexdigest(),
        cwd=str(prepared.cwd),
        spawn_lock_path=spawn_receipt.session_lock_path(home, generation, session)
        .relative_to(home)
        .as_posix(),
        receipt_path=spawn_receipt.receipt_path(home, generation, session, nonce)
        .relative_to(home)
        .as_posix(),
        recorded_at=datetime.now(UTC),
    )


def _write_starting(
    generation: str,
    journal: NormalReleaseRecoveryJournal,
    attempt: SpawnAttempt,
    replaces: SpawnVerdict | None,
) -> NormalReleaseRecoveryJournal:
    """Record the attempt BEFORE its fork (I1), with its displaced verdict (I8)."""
    return _write_normal_journal(
        generation,
        journal.model_copy(
            update={
                "stage": "starting",
                "starting_session": attempt.session,
                "starting_attempt": attempt,
                "replaces": replaces,
            }
        ),
    )


def _adjudicate_slot_attempt(
    plan: PreparedNormalRelease, generation: str, journal: NormalReleaseRecoveryJournal
) -> tuple[str, SpawnVerdict, SessionRecord | None]:
    """I8 phase zero: adjudicate the single recorded in-flight attempt first.

    Returns ``(session, verdict, record)`` where ``record`` is the adopted
    record for ``spawned_alive``. ``ambiguous`` refuses here with zero journal
    writes: an unadjudicated attempt is never displaced, retried or cleared.
    """
    attempt = journal.starting_attempt
    session = journal.starting_session
    prepared = next((item for item in plan.services if item.identity.session == session), None)
    if attempt is None or session is None or prepared is None:
        raise ReleaseRejectedError("normal recovery slot does not name a pinned service")
    home = Path(plan.request.unit.home)
    if (
        attempt.cmd_digest != hashlib.sha256(normal_spawn_command(prepared).encode()).hexdigest()
        or attempt.cwd != str(prepared.cwd)
        or attempt.spawn_lock_path
        != spawn_receipt.session_lock_path(home, generation, session).relative_to(home).as_posix()
        or attempt.receipt_path
        != spawn_receipt.receipt_path(home, generation, session, attempt.nonce)
        .relative_to(home)
        .as_posix()
    ):
        raise ReleaseRejectedError("retained normal attempt does not bind its prepared service")
    remaining = (plan.context.challenge.valid_until - datetime.now(UTC)).total_seconds()
    budget = min(settings.gateway.update_spawn_ambiguity_wait_seconds, max(0.0, remaining / 2))
    outcome = spawn_receipt.await_birth(
        spawn_receipt.receipt_path(home, generation, session, attempt.nonce),
        spawn_receipt.SpawnExpectation(
            nonce=attempt.nonce,
            session=session,
            home=str(home),
            machine=plan.request.unit.machine,
            cmd_digest=attempt.cmd_digest,
            cwd=attempt.cwd,
        ),
        spawn_receipt.session_lock_path(home, generation, session),
        deadline=time.monotonic() + budget,
    )
    if outcome.verdict == "ambiguous":
        raise ReleaseRejectedError(f"in-flight normal attempt is ambiguous: {outcome.reason}")
    if outcome.verdict != "spawned_alive":
        return session, outcome.verdict, None
    if outcome.receipt is None:  # unreachable by construction; fail closed
        raise ReleaseRejectedError("in-flight normal attempt is alive without its receipt")
    record = adopt_birth_record(home, prepared, outcome.receipt, generation=generation)
    return session, "spawned_alive", record


def _record_is_live_candidate(prepared: PreparedService, record: SessionRecord) -> bool:
    """Whether a retained record is the already-ready normal candidate service.

    The record must name the exact prepared command and cwd, and its exact
    process (pid + birth) must still be alive — the adoption evidence for a
    service an earlier pass already started. Identity resolution follows
    ``observe_session``: ``/proc`` starttime exact when present, the
    create-time tolerance otherwise.
    """
    if record.cmd != normal_spawn_command(prepared) or record.cwd != str(prepared.cwd):
        return False
    return (
        observe_process(
            ExpectedProcess(
                pid=record.pid, create_time=record.create_time, starttime=record.starttime
            )
        )
        == "alive"
    )


def _run_service_roster(
    conn: psycopg.Connection,
    plan: PreparedNormalRelease,
    generation: str,
    journal: NormalReleaseRecoveryJournal,
    selector: SelectorReadback,
) -> NormalReleaseRecoveryJournal:
    """I8 adjudication, then the pinned roster: adopt, spawn, observe.

    Phase zero adjudicates the journal's single in-flight attempt before
    anything else; the pinned loop then treats each service in its prepared
    order — a live exact record is re-observed, anything else gets exactly one
    new gated attempt whose ``starting`` write carries the displaced attempt's
    verdict as its ``replaces`` witness. Each service performs at most one
    spawn per pass: a failed spawn or a service that exits before readiness
    refuses the release (that decision belongs to the operator path).
    """
    adopted_session: str | None = None
    adopted_record: SessionRecord | None = None
    slot_verdict: SpawnVerdict | None = None
    if journal.starting_session is not None:
        adopted_session, slot_verdict, adopted_record = _adjudicate_slot_attempt(
            plan, generation, journal
        )
    home = Path(plan.request.unit.home)
    results: list[NormalServiceReadback] = []
    for prepared in plan.services:
        session = prepared.identity.session
        if session == adopted_session and adopted_record is not None:
            # Adopted in phase zero; `slot_verdict` may have moved on from
            # later spawns, so the adopted record itself is the discriminator.
            record = adopted_record
        else:
            try:
                record = spawn_receipt.read_session_record(home, session)
            except spawn_receipt.SpawnEvidenceInvalidError as exc:
                raise ReleaseRejectedError(f"normal service record is damaged: {exc}") from exc
            if record is None or not _record_is_live_candidate(prepared, record):
                attempt = _attempt_for(plan, generation, prepared)
                journal = _write_starting(generation, journal, attempt, slot_verdict)
                results.append(
                    start_normal_service(
                        conn,
                        plan.context,
                        selector,
                        prepared,
                        attempt=attempt,
                        generation=generation,
                    )
                )
                slot_verdict = "spawned_alive"
                continue
        results.append(await_normal_service_ready(conn, plan.context, selector, prepared, record))
    readback = UnitActivationReadback(
        selector=selector,
        services=tuple(sorted(results, key=lambda item: item.service.session)),
    )
    return _write_normal_journal(
        generation,
        journal.model_copy(
            update={
                "stage": "observed",
                "starting_session": None,
                "starting_attempt": None,
                "replaces": None,
                "readback": readback,
            }
        ),
    )


def _completed_readback(journal: NormalReleaseRecoveryJournal) -> UnitActivationReadback:
    if journal.readback is None:  # unreachable by validator; fail closed
        raise ReleaseRejectedError("completed normal recovery lacks its readback")
    return journal.readback


def _land_readback(
    conn: psycopg.Connection, plan: PreparedNormalRelease, journal: NormalReleaseRecoveryJournal
) -> UnitActivationReadback:
    """Land the retained readback in the pending journal; the database wins (§5.4).

    The journal write precedes the database write, so a database entry for this
    unit is the authoritative one and must equal the journal's exactly (the
    silent-replacement refusal is never bypassed). With no database entry the
    journal's readback is the unique one and is landed under fresh authority —
    ``record_pending_unit_readback`` revalidates freshness itself, so an aged
    readback refuses instead of being refreshed. A publication that already
    consumed this pending operation (the all-unit commit) ends the landing
    phase; the commit step owns the current-publication check.
    """
    readback = _completed_readback(journal)
    context = plan.context
    with pending_transaction(conn, context):
        state = _locked_publication(conn)
    pending = state.pending
    if pending is None:
        current = state.current
        if (
            current is None
            or current.operation != context.operation
            or current.activation_challenge != context.challenge.challenge
        ):
            raise ReleaseRejectedError("normal readback has no matching publication")
        return readback
    if pending.operation != context.operation or pending.challenge != context.challenge.challenge:
        raise ReleaseRejectedError("normal readback belongs to another pending operation")
    with pending_transaction(conn, context):
        retained = read_pending_unit_readbacks(conn, context.operation, context.challenge.challenge)
    ours = tuple(item for item in retained if item.selector.unit == plan.request.unit)
    if len(ours) > 1:
        raise ReleaseRejectedError("normal readback appears more than once in the pending journal")
    if ours:
        if ours[0] != readback:
            raise ReleaseRejectedError("database readback differs from the retained readback")
        return readback
    with pending_transaction(conn, context):
        record_pending_unit_readback(conn, context.operation, context.challenge.challenge, readback)
    return readback


def _drive_checked_normal_release(
    plan: PreparedNormalRelease, generation: str
) -> UnitActivationReadback:
    """The checked stage machine: reconcile first, perform only missing effects.

    Every entry runs this same code. The retained journal (if any) decides the
    stage; each stage re-proves what its predecessors recorded, skips what is
    already done, and writes its completion evidence as the last step — so a
    crash anywhere re-enters at a recorded stage and converges without extra
    effects. Fresh authority gates every effect; every wait is bounded by the
    prepared challenge and never renews it.
    """
    home = Path(plan.request.unit.home)
    if settings.general.ava_home.resolve() != home.resolve():
        raise ReleaseRejectedError("normal updater session namespace differs from prepared unit")
    context = plan.context
    operation = context.operation
    challenge = context.challenge.challenge
    remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds())
    if remaining < 2:
        raise ReleaseRejectedError("normal release has no connection budget for its effects")
    previous = (
        plan.request.previous_selector.encode()
        if plan.request.previous_selector is not None
        else None
    )
    journal = _retained_normal_journal(plan, generation)
    if journal is None:
        journal = _write_normal_journal(
            generation,
            NormalReleaseRecoveryJournal(
                request_path=str(plan.request_path),
                operation_context=_prepared_recovery(context),
                unit=plan.request.unit,
                previous_selector=plan.request.previous_selector,
                stage="waiting",
            ),
        )
    with psycopg.connect(
        plan.projection.db_url.get_secret_value(),
        autocommit=True,
        prepare_threshold=None,
        connect_timeout=min(5, remaining),
    ) as conn:
        if journal.stage == "waiting":
            with pending_transaction(conn, context):
                record_pending_migration(conn, operation, challenge)
            select_pending_release(conn, context, plan.request.unit, previous)
            journal = _write_normal_journal(
                generation, journal.model_copy(update={"stage": "selected"})
            )
        if journal.stage == "selected":
            select_pending_release(conn, context, plan.request.unit, previous)
            _stop_bootstrap_checked(plan)
            journal = _write_normal_journal(
                generation, journal.model_copy(update={"stage": "bootstrap_stopped"})
            )
        if journal.stage in {"bootstrap_stopped", "starting"}:
            selector = select_pending_release(conn, context, plan.request.unit, previous)
            journal = _run_service_roster(conn, plan, generation, journal, selector)
        if journal.stage == "observed":
            return _land_readback(conn, plan, journal)
        if journal.stage != "committed":
            raise ReleaseRejectedError("normal release reached an unexpected recovery stage")
        return _completed_readback(journal)


def commit_normal_release_after_publication(
    plan: PreparedNormalRelease, generation: str
) -> UnitActivationReadback:
    """The per-unit commit step: verify the all-unit publication, record ``committed``.

    Driven by the coordinator's per-unit invocation in the wiring phase (not
    wired in this batch). The evidence is the current publication — same
    operation and challenge, covering this unit — with no pending publication
    left; the unit's readback was consumed by the coordinator's
    ``commit_current``. Idempotent: a committed journal returns unchanged.
    """
    context = plan.context
    journal = _retained_normal_journal(plan, generation)
    if journal is None:
        raise ReleaseRejectedError("normal commit has no retained recovery journal")
    if journal.stage == "committed":
        return _completed_readback(journal)
    if journal.stage != "observed":
        raise ReleaseRejectedError("normal commit requires the completed unit readback")
    remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds())
    if remaining < 2:
        raise ReleaseRejectedError("normal commit has no connection budget")
    with psycopg.connect(
        plan.projection.db_url.get_secret_value(),
        autocommit=True,
        prepare_threshold=None,
        connect_timeout=min(5, remaining),
    ) as conn:
        with pending_transaction(conn, context):
            state = _locked_publication(conn)
        current = state.current
        if (
            state.pending is not None
            or current is None
            or current.operation != context.operation
            or current.activation_challenge != context.challenge.challenge
            or plan.request.unit not in current.units
        ):
            raise ReleaseRejectedError("normal commit requires the exact current publication")
    journal = _write_normal_journal(generation, journal.model_copy(update={"stage": "committed"}))
    return _completed_readback(journal)


def execute_normal_release(plan: PreparedNormalRelease, generation: str) -> int:
    """The checked activation entry: drives the checked chain to completion.

    The activation fence (design §7.1) was removed by the flip that declared
    ``CHECKED_ACTIVATION_READY`` in this module; every entry now reconciles and
    performs only the missing effects under fresh authority. Completion reports
    the process code ``0`` (the pre-fence contract).
    """
    _drive_checked_normal_release(plan, generation)
    return 0
