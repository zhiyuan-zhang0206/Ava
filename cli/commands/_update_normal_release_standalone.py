"""The standalone death-continuation entry for the checked normal release.

A unit whose old orchestrator died mid-handoff re-enters here: the standalone
preparation validates the retained local identities (exited predecessor, the
retained handoff, the candidate-ready bootstrap envelope, and the selector
predecessor or its prepared pointer), then ``run_normal_release`` takes host
mutual exclusion, re-claims the exact generation (only after positive
owner-death evidence), and hands the plan to the checked activation entry in
``cli.commands._update_normal_release``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import PreparedService, prepare_normal_services
from cli.commands._update_bootstrap import BootstrapHopRequest, _private_reference, probe_bootstrap
from cli.commands._update_normal_release import (
    NormalReleaseRequest,
    PreparedNormalRelease,
    _candidate_ready_recovery,
    _preflight_pending_plan,
    _read_ops_record,
    commit_normal_release_after_publication,
    execute_normal_release,
)
from services.agent_ops.bootstrap import (
    ObserverProjection,
    read_prepared_context,
    validate_operation,
)
from shared import ui_update_state, updater_handoff
from shared.host_deploy_state import release_updater_lock, try_acquire_updater_lock
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.runtime_release import ReleaseRejectedError
from shared.session_record import SessionRecord
from shared.verified_file import regular_bytes


def _handoff_owner_is_dead_lineage(
    handoff: updater_handoff.UpdaterHandoffSnapshot, predecessor: ExpectedProcess
) -> bool:
    """Whether a proven-dead handoff owner belongs to this operation's lineage.

    The nominal shape is the old orchestrator itself. Late-stage recovery adds
    the process that already re-took the handoff and then died — the hop
    updater session, or a prior standalone ``direct-updater`` claim — which is
    exactly the deep-crash state the standalone entry must reach. The caller
    has already required positive death evidence via ``owner_is_live``; this
    only narrows *which* dead owners count.
    """
    if (
        handoff.owner_pid == predecessor.pid
        and handoff.owner_create_time == predecessor.create_time
    ):
        return True
    from shared.cluster import session_name

    session = handoff.expected_session or ""
    return session == session_name("updater") or (
        session.startswith("direct-updater:pid")
        and session.removeprefix("direct-updater:pid").isdigit()
    )


def prepare_normal_release(path: Path, *, for_commit: bool = False) -> PreparedNormalRelease:
    """Validate all local support/identity inputs while the old observer serves.

    ``for_commit`` drops the stop-stage faces -- the service-roster preparation,
    the live-bootstrap probe and the pending-plan preflight -- because the
    commit tail runs after the all-unit publication: the pending journal is
    consumed and the unit serves its normal services (`ava-ops` is no longer
    the candidate observer). Every other check re-derives identically.
    """
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
        or updater_handoff.owner_is_live(handoff)
        or not _handoff_owner_is_dead_lineage(handoff, request.predecessor)
    ):
        raise ReleaseRejectedError("existing handoff does not identify a dead lineage owner")
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
    services: tuple[PreparedService, ...] = ()
    if not for_commit:
        services = prepare_normal_services(request.unit, context.schema_digest)
        if not any(service.identity.session == "ava-ops" for service in services):
            raise ReleaseRejectedError("unit has no normal same-endpoint ops service")
    previous = request.previous_selector.encode() if request.previous_selector is not None else None
    # Late-stage recovery may find the pointer already committed to the
    # prepared release; the checked chain re-selects idempotently, so both the
    # predecessor and the prepared pointer are exact, and anything else refuses.
    if read_selector(home) not in (previous, selector_bytes(request.unit)):
        raise ReleaseRejectedError(
            "normal selector differs from the predecessor and the prepared pointer"
        )
    projection = ObserverProjection.from_environment()
    validate_operation(context, projection)
    bootstrap: SessionRecord | None = None
    if not for_commit:
        bootstrap = _read_ops_record(home)
        # probe_bootstrap challenges the *live* process (real command, actual
        # self-report, native ownership). A bootstrap that is already gone is a
        # deep-crash recovery state, not a refusal: the stop stage re-reads this
        # exact retained record and adjudicates — exited/identity_mismatch count
        # as already stopped, and anything unobservable refuses there.
        verdict = observe_process(
            ExpectedProcess(
                pid=bootstrap.pid,
                create_time=bootstrap.create_time,
                starttime=bootstrap.starttime,
            )
        )
        if verdict == "alive":
            probe_bootstrap(context, projection)
    prepared = PreparedNormalRelease(
        path, request, context, projection, services, bootstrap, handoff.generation
    )
    if not for_commit:
        _preflight_pending_plan(prepared)
    return prepared


def _reclaim_generation_and_enter(
    plan: PreparedNormalRelease, enter: Callable[[PreparedNormalRelease, str], object]
) -> None:
    """Claim the retained handoff (only after owner death), enter, dispose.

    Mutual exclusion first; the exact generation is re-taken by
    ``resume_bootstrap``, which itself refuses a live owner. ``clear`` runs
    only when the claim succeeded, and it CAS-checks its own preconditions
    before dropping retained state.
    """
    if not try_acquire_updater_lock():
        raise ReleaseRejectedError("another updater holds this unit")
    generation: str | None = None
    claimed = False
    try:
        expected = f"direct-updater:pid{os.getpid()}"
        generation = plan.resume_generation
        with ui_update_state.lifecycle_lock():
            if not updater_handoff.resume_bootstrap(generation, expected_session=expected):
                raise ReleaseRejectedError("normal updater could not claim its existing handoff")
            claimed = True
        enter(plan, generation)
    finally:
        if generation is not None and claimed:
            with contextlib.suppress(Exception), ui_update_state.lifecycle_lock():
                updater_handoff.clear(generation)
        release_updater_lock()


def run_normal_release(path: Path) -> int:
    """The drive entry: the coordinator's per-unit continuation dispatch.

    Also the death-recovery re-entry for a unit whose old orchestrator died
    mid-handoff: the preparation validates the retained local identities, the
    claim re-takes the exact generation, and the checked activation entry
    drives the stage machine (task #4129 I6: the coordinator dispatches this
    entry; it never continues in-process from the hop).
    """
    plan = prepare_normal_release(path)
    _reclaim_generation_and_enter(plan, execute_normal_release)
    return 0


def run_normal_commit(path: Path) -> int:
    """The commit-tail entry: record ``committed`` after the all-unit commit.

    Dispatched per unit by the coordinator after the publication commit (task
    #4129 I6). The commit-variant preparation skips the stop-stage faces (the
    roster, the live-bootstrap probe, the pending-plan preflight): the
    publication has already consumed the pending journal. No activation fence
    here: the entry is structurally downstream-only --
    ``commit_normal_release_after_publication`` refuses unless the retained
    journal sits at ``observed``, which only the checked drive chain (under its
    own gates) produces. At the committed stage the retained bootstrap envelope
    becomes clearable, so this exit disposes it.
    """
    plan = prepare_normal_release(path, for_commit=True)
    _reclaim_generation_and_enter(plan, commit_normal_release_after_publication)
    return 0
