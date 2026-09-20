"""Coordinator-side collection of the managed-writer closure (task #4129, channel D).

The post-stop half of a managed-writer rollout: once every unit's restricted hop
has reached `candidate_ready` (the wait below), this module gathers each unit's
observer facts and hop ledger over the units' own restricted endpoints,
re-derives every binding from the shipped bytes and the served payloads --
nothing is trusted as a claim -- assembles the fleet's single positive closure
and adopts it into the durable pending journal through the existing seat's
fresh, locked revalidation.

The whole module is deliberately heavy (httpx, the database, the ops daemon's
own schemas) and must stay behind method-local imports: the eagerly-loaded
updater closure (`import cli.commands` + `_update_agent_runner`) must not reach
`shared.session_backend` / `shared.session_record`, and this module's chain
reaches both through `services.agent_ops.bootstrap` and `shared.hop_ledger`.
Only `cli.commands._managed_writer_hop`'s light types cross the boundary.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

import httpx
from pydantic import AwareDatetime

from cli.commands._managed_writer_hop import CollectorInput, CollectorUnitInput
from services.agent_ops.bootstrap import BootstrapRuntimeIdentity, PreparedObservation
from shared import http_dial, machines
from shared.cluster_auth import bearer_header
from shared.config import settings
from shared.db import connect as db_connect
from shared.db_transaction import write_transaction
from shared.hop_ledger import (
    LEDGER_MODE,
    MAX_LEDGER_BYTES,
    SessionRecordSummary,
    envelope_bytes,
)
from shared.machine import machine_name
from shared.managed_writer_barrier import (
    Digest,
    EvidenceModel,
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    RolloutIdentity,
)
from shared.managed_writer_closure import assemble_collection, assemble_unit_closure
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ProcessVerdict,
    SessionVerdict,
    observe_process,
)
from shared.managed_writer_publication import WriterPublication, adopt_pending_collection
from shared.native_job_observation import LauncherObservation
from shared.updater_recovery import BootstrapRecoveryJournal

_MAX_PULL_BODY_BYTES = 2 * MAX_LEDGER_BYTES
"""The coordinator's read bound for one unit response (512 KiB = the ledger's own
256 KiB write budget plus JSON headroom): the ledger slot is the largest legal
payload this module reads, so a substituted endpoint must never make the
coordinator buffer without bound (the `probe_bootstrap` budget discipline)."""


class CollectorRefusal(RuntimeError):  # noqa: N818 — refusal verdict naming; CollectorTransferError carries the retryable-transfer condition.
    """The collection cannot proceed from what the unit(s) served or shipped."""


class CollectorTransferError(RuntimeError):
    """One unit read failed transiently (unreachable, oversized, malformed wire)."""


@dataclass(frozen=True)
class JournaledRegistration:
    """The pending journal's own begin registration (task #4129 I4, F1/N3).

    ``valid_until`` is the window V the begin that opened the journal sealed;
    ``plan_digest`` is that execution's sealed-plan digest. Both are None when
    no same-operation pending journal is registered (first run, or a journal
    written before the registration existed).
    """

    valid_until: datetime | None
    plan_digest: Digest | None


class UnitObservationRead(EvidenceModel):
    """One unit's `bootstrap-observation` response, re-derived from the wire.

    The observer's permanent semantics: it reports facts only -- `closure`
    stays its `unknown` literal, and the positive closure is this module's to
    derive from what the read carries.
    """

    mode: str
    full_ready: bool
    challenge: UUID
    observer_instance: UUID
    unit: ManagedUnit
    observed_at: AwareDatetime
    processes: tuple[ProcessVerdict, ...]
    sessions: tuple[SessionVerdict, ...]
    launchers: tuple[LauncherObservation, ...]
    closure: str
    runtime: BootstrapRuntimeIdentity


class UnitLedgerRead(EvidenceModel):
    """One unit's `bootstrap-hop-ledger` response, re-derived from the wire.

    A damaged slot stays a 200 read: `journal_present` / `journal_readable`
    encode it, and the envelope fields (with their raw-byte `payload_digest`)
    appear only when readable.
    """

    mode: str
    challenge: UUID
    journal_present: bool
    journal_readable: bool
    version: int | None = None
    generation: str | None = None
    journal: dict[str, object] | None = None
    payload_digest: Digest | None = None
    boot_id: UUID | None = None
    session_record: SessionRecordSummary


def read_journaled_registration(operation: RolloutIdentity) -> JournaledRegistration:
    """Read the pending journal's begin registration for `operation`, if any.

    One short autocommit read outside any lock: the begin position adopts the
    journaled window V (and reports its plan digest as audit evidence) so a
    same-operation retry re-seals against the window that was registered first
    -- the sealed window cannot slide under a later execution.
    """
    with db_connect(autocommit=True) as conn:
        row = conn.execute(
            "SELECT managed_writer_evidence FROM deployment_state WHERE id=1"
        ).fetchone()
    if row is None or row[0] is None:
        return JournaledRegistration(valid_until=None, plan_digest=None)
    publication = WriterPublication.model_validate_json(json.dumps(row[0]))
    pending = publication.pending
    if pending is None or pending.operation != operation:
        return JournaledRegistration(valid_until=None, plan_digest=None)
    return JournaledRegistration(valid_until=pending.valid_until, plan_digest=pending.plan_digest)


def wait_for_candidate_ready(collector: CollectorInput) -> str | None:
    """Wait until every unit's hop journal reads `candidate_ready`.

    Polls each not-yet-ready unit's bounded ledger read at the configured
    cadence; the sealed window V bounds the wait (a unit that has not reached
    the stage by then fails the rollout rather than sliding the window). A
    transfer failure keeps the wait alive (the unit may still be booting) --
    its last stage string carries the error into the deadline detail. Returns
    None on success, or the failing detail -- a refusal, a unit that recovered
    its predecessor instead, or the deadline -- for the verdict to report.
    """
    try:
        valid_until, _plan_digest = _window_and_plan(collector)
    except CollectorRefusal as exc:
        return f"the hop wait refused: {exc}"
    poll_seconds = settings.gateway.update_managed_writer_hop_poll_seconds
    timeout_s = settings.gateway.update_managed_writer_pull_timeout_seconds
    ordered_machines = sorted(unit.machine for unit in collector.units)
    stages: dict[str, str] = {}
    ready: set[str] = set()
    while True:
        for unit in collector.units:
            if unit.machine in ready:
                continue
            if datetime.now(UTC) >= valid_until:
                return _deadline_detail(stages, ordered_machines)
            try:
                payload = _pull(
                    unit,
                    "bootstrap-hop-ledger",
                    challenge=collector.challenge,
                    timeout=timeout_s,
                    what="the hop ledger read",
                )
                stage = _ledger_stage(payload, collector.challenge)
            except CollectorTransferError as exc:
                stage = str(exc)
            except CollectorRefusal as exc:
                return f"the hop wait refused: {exc}"
            if stages.get(unit.machine) != stage:
                stages[unit.machine] = stage
                print(f"  \u00b7 managed-writer wait: {unit.machine} {stage}")
            if stage == "candidate_ready":
                ready.add(unit.machine)
            elif stage == "recovered":
                return (
                    f"the hop wait: {unit.machine} recovered its predecessor "
                    "instead of reaching candidate-ready"
                )
        if len(ready) == len(collector.units):
            print(f"  \u2713 managed-writer wait: all {len(collector.units)} units candidate-ready")
            return None
        remaining = (valid_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return _deadline_detail(stages, ordered_machines)
        time.sleep(min(poll_seconds, remaining))


def collect_and_adopt(collector: CollectorInput) -> int:
    """Gather every unit's closure, assemble the collection, adopt it; 0 or 1.

    One pass: per unit re-derive the expected writers from the sealed
    candidate-context bytes, read the running observer and the hop ledger,
    re-derive every binding (echoed challenge, digests, window, runtime
    identity), assemble the unit's positive closure and then the fleet's single
    collection, and adopt it through the existing seat's fresh, locked
    revalidation. Any refusal prints and returns 1 with the pending journal
    retained -- the fail-closed default is "collect again", never "publish what
    we have".
    """
    try:
        valid_until, plan_digest = _window_and_plan(collector)
    except CollectorRefusal as exc:
        print(f"\n\u2717 managed-writer collect refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"  \u00b7 managed-writer collect: window V={valid_until.isoformat()} "
        f"(plan_digest={plan_digest or 'none'})"
    )
    timeout_s = settings.gateway.update_managed_writer_pull_timeout_seconds
    coordinator = machine_name()
    closures: list[ManagedUnitClosure] = []
    expected_units: list[ManagedUnit] = []
    for unit in collector.units:
        try:
            expected = _expected_writers(unit)
            observation = _pull(
                unit,
                "bootstrap-observation",
                challenge=collector.challenge,
                timeout=timeout_s,
                what="the observer read",
            )
            ledger = _pull(
                unit,
                "bootstrap-hop-ledger",
                challenge=collector.challenge,
                timeout=timeout_s,
                what="the hop ledger read",
            )
            closure = accept_unit(
                unit,
                expected,
                observation,
                ledger,
                operation=collector.operation,
                challenge=collector.challenge,
                valid_until=valid_until,
                coordinator_machine=coordinator,
            )
        except (CollectorRefusal, CollectorTransferError) as exc:
            print(f"  \u2717 {unit.machine}: {exc}", file=sys.stderr)
            return 1
        print(f"  \u2713 {unit.machine}: positive closure assembled")
        closures.append(closure)
        expected_units.append(
            ManagedUnit(
                machine=unit.machine,
                home=unit.home,
                inventory_digest=unit.prepared_receipt_digest,
            )
        )
    collection = assemble_collection(
        operation=collector.operation,
        candidate_digest=collector.candidate_digest,
        challenge=collector.challenge,
        collected_at=datetime.now(UTC),
        valid_until=valid_until,
        expected_units=tuple(expected_units),
        closures=tuple(closures),
    )
    if collection is None:
        print(
            "\n\u2717 managed-writer collect refused: the units did not assemble "
            "one complete collection inside the sealed window",
            file=sys.stderr,
        )
        return 1
    try:
        with write_transaction() as conn:
            adopt_pending_collection(conn, collection)
    except ManagedWriterBarrierError as exc:
        print(
            f"\n\u2717 managed-writer collect refused: {exc}\n"
            "  the pending journal remains; run `ava cluster recover-pending`",
            file=sys.stderr,
        )
        return 1
    print(f"  \u2713 managed-writer collection adopted ({len(closures)} units)")
    return 0


def accept_unit(  # noqa: PLR0915 — one ordered evidence re-derivation; every check is one refusal.
    unit: CollectorUnitInput,
    expected: ExpectedUnitWriters,
    observation: dict[str, object],
    ledger: dict[str, object],
    *,
    operation: RolloutIdentity,
    challenge: UUID,
    valid_until: datetime,
    coordinator_machine: str,
) -> ManagedUnitClosure:
    """Re-derive one unit's positive closure from its served facts.

    Every binding is recomputed here, never accepted as a claim: the echoed
    challenge, the observation window, the expected-writers identity (from the
    sealed candidate-context bytes), the runtime image identity (with the
    local resolve/alive checks only for the coordinator's own unit), the
    ledger's envelope digest recomputation, the exact session-record process
    identity, the four journaled digests against the bytes this coordinator
    dispatched, and finally the closure derivation where those facts meet the
    journaled launcher terminals. Anything unknown, drifted or malformed
    raises `CollectorRefusal`.
    """
    try:
        read = UnitObservationRead.model_validate_json(json.dumps(observation))
    except ValueError as exc:
        raise CollectorRefusal("the observer read is not its wire shape") from exc
    if read.mode != "bootstrap_observation":
        raise CollectorRefusal("the observer read belongs to another mode")
    if read.full_ready is not False:
        raise CollectorRefusal("the observer is not the restricted pre-publication observer")
    if read.closure != "unknown":
        raise CollectorRefusal("the observer claimed a closure it cannot own")
    if read.challenge != challenge:
        raise CollectorRefusal("the observer read echoes another challenge")
    if read.unit != expected.unit():
        raise CollectorRefusal("the observer read describes another unit inventory")
    if not operation.acquired_at <= read.observed_at < valid_until:
        raise CollectorRefusal("the observation is outside the sealed window")
    runtime = read.runtime
    if (
        runtime.home != expected.home
        or runtime.artifact_digest != expected.artifact_digest
        or runtime.manifest_digest != expected.manifest_digest
    ):
        raise CollectorRefusal("the observer runtime belongs to another image identity")
    image_root = PurePosixPath(expected.home) / "releases" / expected.artifact_digest
    module = PurePosixPath(runtime.module)
    if (
        not module.is_absolute()
        or str(module) != runtime.module
        # Lexical containment alone accepts `venv/../../..`; the local branch's
        # resolve(strict) refuses it, the remote branch needs this clause (QA N1).
        or ".." in module.parts
        or not module.is_relative_to(image_root / "venv")
    ):
        raise CollectorRefusal("the observer runtime is not loaded from its prepared image")
    if unit.machine == coordinator_machine:
        try:
            resolved = Path(runtime.module).resolve(strict=True)
        except OSError as exc:
            raise CollectorRefusal("the observer module path cannot be resolved locally") from exc
        if resolved != Path(runtime.module):
            raise CollectorRefusal("the observer module path is not its own canonical path")
        if observe_process(runtime.process) != "alive":
            raise CollectorRefusal("the observer process is not alive")
    try:
        ledger_read = UnitLedgerRead.model_validate_json(json.dumps(ledger))
    except ValueError as exc:
        raise CollectorRefusal("the hop ledger is not its wire shape") from exc
    if ledger_read.mode != LEDGER_MODE:
        raise CollectorRefusal("the hop ledger belongs to another read mode")
    if ledger_read.challenge != challenge:
        raise CollectorRefusal("the hop ledger echoes another challenge")
    journal_body = ledger_read.journal
    if not ledger_read.journal_present or not ledger_read.journal_readable or journal_body is None:
        raise CollectorRefusal("the unit has no readable hop journal to collect")
    if ledger_read.boot_id is None:
        raise CollectorRefusal("the hop ledger carries no boot identity")
    summary = ledger_read.session_record
    if summary.state != "ok" or summary.pid is None or summary.create_time is None:
        raise CollectorRefusal("the ops session record does not prove its process identity")
    recorded = ExpectedProcess(
        pid=summary.pid, create_time=summary.create_time, starttime=summary.starttime
    )
    if recorded != runtime.process:
        raise CollectorRefusal("the ops session record is not the observer process")
    if ledger_read.version != 1:
        raise CollectorRefusal("the hop ledger envelope version is unsupported")
    generation = ledger_read.generation
    payload_digest = ledger_read.payload_digest
    if generation is None or payload_digest is None:
        raise CollectorRefusal("the hop ledger envelope is incomplete")
    recomputed = hashlib.sha256(envelope_bytes(1, generation, journal_body)).hexdigest()
    if recomputed != payload_digest:
        raise CollectorRefusal("the hop ledger bytes do not recompute their payload digest")
    try:
        journal = BootstrapRecoveryJournal.model_validate_json(json.dumps(journal_body))
    except ValueError as exc:
        raise CollectorRefusal("the hop journal does not parse as its schema") from exc
    if journal.stage != "candidate_ready":
        raise CollectorRefusal("the hop journal is not at candidate-ready")
    if journal.normal_release_planned:
        raise CollectorRefusal("the hop journal plans a normal release")
    if journal.request_digest != hashlib.sha256(unit.request).hexdigest():
        raise CollectorRefusal("the journal's hop request does not match the dispatched bytes")
    if journal.candidate_context_digest != hashlib.sha256(unit.candidate_context).hexdigest():
        raise CollectorRefusal(
            "the journal's candidate context does not match the dispatched bytes"
        )
    if journal.recovery_context_digest != hashlib.sha256(unit.recovery_context).hexdigest():
        raise CollectorRefusal("the journal's recovery context does not match the shipped bytes")
    if journal.inventory_digest != unit.prepared_receipt_digest:
        raise CollectorRefusal("the journal's inventory receipt does not match the sealed receipt")
    closure = assemble_unit_closure(
        expected,
        operation=operation,
        operation_challenge=challenge,
        echoed_challenge=read.challenge,
        boot_id=ledger_read.boot_id,
        observer_instance=read.observer_instance,
        observed_unit=read.unit,
        prepared_receipt_digest=unit.prepared_receipt_digest,
        observed_at=read.observed_at,
        valid_until=valid_until,
        processes=read.processes,
        sessions=read.sessions,
        launchers=read.launchers,
        terminals=journal.launcher_terminals,
    )
    if closure is None:
        raise CollectorRefusal("the unit's facts do not meet a positive closure")
    return closure


def _expected_writers(unit: CollectorUnitInput) -> ExpectedUnitWriters:
    """The unit's sealed expected writers, parsed from the dispatched context bytes."""
    try:
        context = PreparedObservation.model_validate_json(unit.candidate_context)
    except ValueError as exc:
        raise CollectorRefusal("the sealed candidate context does not parse") from exc
    expected = context.expected
    if expected.machine != unit.machine or expected.home != unit.home:
        raise CollectorRefusal("the sealed candidate context describes another unit")
    return expected


def _window_and_plan(collector: CollectorInput) -> tuple[datetime, Digest | None]:
    """The operation's window V, journaled registration first, sealed context second.

    The journal's registration is authoritative once present (the begin adopted
    it too, so a retry cannot slide the window); a journal without one falls
    back to the sealed candidate contexts' single challenge window. Returns the
    audit plan digest beside V; no sealed source refuses.
    """
    registration = read_journaled_registration(collector.operation)
    if registration.valid_until is not None:
        return registration.valid_until, registration.plan_digest
    sealed = _sealed_window(collector)
    if sealed is None:
        raise CollectorRefusal(
            "no observation window is available for this operation (the journal "
            "has no registration and no sealed candidate context carries one)"
        )
    return sealed, None


def _sealed_window(collector: CollectorInput) -> datetime | None:
    """The single challenge window across the sealed contexts, or None."""
    windows: set[datetime] = set()
    for unit in collector.units:
        try:
            context = PreparedObservation.model_validate_json(unit.candidate_context)
        except ValueError:
            continue
        windows.add(context.challenge.valid_until)
    if len(windows) != 1:
        return None
    return next(iter(windows))


def _deadline_detail(stages: Mapping[str, str], ordered_machines: Sequence[str]) -> str:
    pairs = ", ".join(
        f"{machine}={stages.get(machine, 'not observed')}" for machine in ordered_machines
    )
    return f"the hop wait hit the sealed window with units not candidate-ready: {pairs}"


def _ledger_stage(payload: dict[str, object], challenge: UUID) -> str:
    """One ledger read's stage; a foreign read refuses, damage reads as a stage."""
    try:
        read = UnitLedgerRead.model_validate_json(json.dumps(payload))
    except ValueError as exc:
        raise CollectorRefusal("the hop ledger is not its wire shape") from exc
    if read.mode != LEDGER_MODE:
        raise CollectorRefusal("the hop ledger read belongs to another mode")
    if read.challenge != challenge:
        raise CollectorRefusal("the hop ledger read echoes another challenge")
    if not read.journal_present:
        return "absent"
    journal_body = read.journal
    if not read.journal_readable or journal_body is None:
        return "unreadable"
    stage = journal_body.get("stage")
    if not isinstance(stage, str):
        return "unreadable"
    return stage


def _pull(
    unit: CollectorUnitInput,
    endpoint: str,
    *,
    challenge: UUID,
    timeout: float,
    what: str,
) -> dict[str, object]:
    """One bounded challenge read against a unit's restricted ops surface.

    A 409 is the challenge's own refusal (unknown or expired, fail-closed); any
    other non-200 answer is a transfer failure for the caller's retry or
    refusal ladder.
    """
    base = _unit_base_url(unit)
    status, payload = _post_read(base, endpoint, what, challenge=challenge, timeout=timeout)
    if status == 200:
        return payload
    if status == 409:
        raise CollectorRefusal(f"{what} refused the challenge (unknown or expired)")
    raise CollectorTransferError(f"{what} answered HTTP {status}")


def _post_read(
    base: str, endpoint: str, what: str, *, challenge: UUID, timeout: float
) -> tuple[int, dict[str, object]]:
    """One POST of the challenge envelope; transport and shape failures are transfers."""
    secret = settings.data_plane.cluster_secret
    headers = {"Content-Type": "application/json"}
    if secret:
        headers.update(bearer_header(secret))
    try:
        response = http_dial.post(
            f"{base}/ops/{endpoint}",
            content=json.dumps({"challenge": str(challenge)}).encode(),
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CollectorTransferError(f"{what}: unreachable ({type(exc).__name__})") from exc
    if len(response.content) > _MAX_PULL_BODY_BYTES:
        raise CollectorTransferError(f"{what}: response exceeds its read bound")
    try:
        parsed: object = json.loads(response.content)
    except ValueError as exc:
        raise CollectorTransferError(f"{what}: response is not a JSON object") from exc
    if not isinstance(parsed, dict):
        raise CollectorTransferError(f"{what}: response is not a JSON object")
    return response.status_code, cast("dict[str, object]", parsed)


def _unit_base_url(unit: CollectorUnitInput) -> str:
    """The unit's ops base URL: the pre-resolved one, else one fresh lookup."""
    if unit.ops_url is not None:
        return unit.ops_url.rstrip("/")
    try:
        return machines.lookup(unit.machine).rstrip("/")
    except (machines.MachineGatewayUrlMissing, machines.MachineNotRegistered) as exc:
        raise CollectorRefusal(f"no ops address is known for {unit.machine}") from exc
