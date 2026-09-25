"""The coordinator-side hop phase: per-unit plans, fan-out, and its gate (task #4129, channel C).

The hop phase starts every unit's restricted updater hop. The begin chain
assembles one `HopUnitPlan` per unit -- its private projections, their
content-named paths, and the retained candidate image whose interpreter runs
the hop (`_managed_writer_dispatch.assemble_phase_inputs`); this module fans the
plan out to each unit's `cluster_bootstrap_hop` op and gates the phase on the
full roster of acknowledgements. The op itself writes nothing: it starts the
detached `ava-updater` session on the candidate image's `--bootstrap-hop`
entry, whose own read-only admission (`prepare_bootstrap_hop`) and
compare-and-set are the authority.

Every import here stays in the light cli closure: the eagerly-loaded wiring
and verdict positions import this module for `HopUnitPlan`, so the
gathered-facts types (and the ops stack behind them) live with the begin chain
instead, and `ops.cluster_rpc` is imported lazily inside the fan-out -- the
`_update_fanout` discipline.

The hop phase adds no resume semantics in this slice (frozen): `phase_b_hops`
returns an empty `hosts_to_resume`, so a hop verdict never compensating-resumes
hosts. After the hop gate the fallback is checked recovery (`ava cluster
recover-pending`); the begin chain hands the closing section its
`ManagedWriterPhaseInput` -- the hop plans above plus the collection phase's
`CollectorInput` (task #4129 I5) -- so the hop wait and the ledger-driven
collection reach this phase without this module importing the collector, whose
chain is heavy and must stay behind method-local imports.

The coordinator-side continuation phase: per-unit drives and commit tails (task #4129, channel E).

The post-publication half of a managed-writer rollout. Two steps, both fanned
out from the begin chain's sealed `ManagedWriterPhaseInput` and both gated on
the sealed window V:

- `drive_continuation` starts every unit's normal-release drive -- the
  restricted updater session that runs the retained candidate image's
  `--normal-release` entry against the unit's sealed `NormalReleaseRequest`
  projection. The drive records its migration receipt and its per-unit
  readback in the durable pending journal; this module then waits until the
  journal reports the FULL roster read back (the commit seat accepts nothing
  less), tolerating the journal's own not-ready refusals as states, not
  failures.
- `commit_tails` runs after the P5 commit: it starts every unit's
  `--normal-commit` tail, whose only work is to record the committed stage
  locally and dispose the retained bootstrap envelope, and waits until each
  unit's recovery slot reads `committed` -- or is absent, which the clear
  gate's own compare-and-set proves can only mean a committed tail that has
  already disposed (the envelope is clearable only at `committed`; a journal
  that never progressed past it cannot be cleared).

A unit whose hand-over session is still exiting answers the dispatch with the
daemon's in-flight refusal; that is the ONE retryable answer (it started
nothing), so the dispatch is re-sent until the session clears, bounded by V.
Every other refusal is deterministic and fails the step at once -- the roster's
whole verdict is printed before the caller decides.

The continuation half's imports stay in the same light cli closure: it is
reached only through the wiring's method-local import, so the ops RPC stack,
the database and the activation module it borrows all load beside the call,
never at `import cli.commands` time."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

from cli.commands._update_recover import RolloutOutcome
from ops.cluster_session import _UPDATER_SERVICE
from ops.ops_bootstrap_hop import BootstrapHopResult, BootstrapRecoveryReadResult
from ops.ops_normal_continue import NormalContinueResult
from ops.rpc_prepare_dispatch import ProjectionFile
from shared.cluster.derive import session_name
from shared.managed_writer_barrier import Digest, ManagedWriterBarrierError, RolloutIdentity


@dataclass(frozen=True)
class HopUnitPlan:
    """One unit's hop dispatch: its projections and the op payload's two paths.

    `request_path` is where the unit will write the hop request (the
    content-addressed name of the request projection); `artifact_digest`
    selects the candidate image whose interpreter runs the hop entry. `ops_url`
    is the pre-resolved ops base URL (None resolves it at dial time).
    """

    machine: str
    home: str
    ops_url: str | None
    artifact_digest: Digest
    request_path: str
    projections: tuple[ProjectionFile, ...]


@dataclass(frozen=True)
class CollectorUnitInput:
    """One unit's collection inputs: the exact bytes its closure must re-derive from.

    `candidate_context` / `request` are the canonical bytes the begin dispatch
    staged under their content names (the unit's restricted hop replays them,
    and the ledger's digests bind the files it read); `recovery_context` is the
    shipped restricted-A context's exact bytes. `normal_request` is the sealed
    `NormalReleaseRequest` projection's exact bytes -- the journal's
    `normal_release_planned` claim is checked against its presence, and the
    sealed request must name it (task #4129 I6). `prepared_receipt_digest` is
    the sealed receipt digest the collection's adoption gate binds.
    """

    machine: str
    home: str
    ops_url: str | None
    candidate_context: bytes
    request: bytes
    recovery_context: bytes
    normal_request: bytes | None
    prepared_receipt_digest: Digest


@dataclass(frozen=True)
class CollectorInput:
    """Everything the collection phase needs to gather one operation's closure.

    `challenge` is the journal's single observation challenge (every unit echo
    must match it); `valid_until` is the begin execution's sealed window V; the
    units are in the sealed (machine, home) order, so an assembled collection
    starts out sorted by construction.
    """

    operation: RolloutIdentity
    challenge: UUID
    valid_until: datetime
    candidate_digest: Digest
    units: tuple[CollectorUnitInput, ...]


@dataclass(frozen=True)
class ContinueUnitInput:
    """One unit's continuation dispatch: the sealed normal entry to start (channel E).

    `request_path` is the content-named `NormalReleaseRequest` projection the
    begin chain staged beside the hop request; `artifact_digest` selects the
    candidate image whose interpreter runs the continuation entry. The
    coordinator's fan-out consumes these (task #4129 I6); the entry re-derives
    every binding from the staged bytes itself.
    """

    machine: str
    home: str
    ops_url: str | None
    artifact_digest: Digest
    request_path: str


@dataclass(frozen=True)
class ManagedWriterPhaseInput:
    """The begin chain's full hand-off: the hop plans plus the collector input.

    One container so the eagerly-loaded wiring / verdict positions carry the hop
    phase, the collection phase and the continuation phase as a single value;
    the collector module itself stays behind method-local imports.
    """

    hop_plans: tuple[HopUnitPlan, ...]
    collector: CollectorInput
    continue_units: tuple[ContinueUnitInput, ...]


@dataclass(frozen=True)
class HopDispatchOutcome:
    """One unit's answer to the hop dispatch."""

    machine: str
    ok: bool
    detail: str


def _hop_outcome(plan: HopUnitPlan, result: dict[str, object]) -> HopDispatchOutcome:
    """One acknowledgement, re-derived from the wire values; never trusted as such."""
    ack = BootstrapHopResult.model_validate_json(json.dumps(result))
    expected_session = session_name(_UPDATER_SERVICE)
    if ack.machine != plan.machine:
        return HopDispatchOutcome(
            machine=plan.machine, ok=False, detail="acknowledgement belongs to another machine"
        )
    if ack.home != plan.home:
        return HopDispatchOutcome(
            machine=plan.machine, ok=False, detail="acknowledgement belongs to another unit home"
        )
    if ack.session != expected_session:
        return HopDispatchOutcome(
            machine=plan.machine,
            ok=False,
            detail=f"spawned {ack.session!r}, not the updater session",
        )
    if PurePosixPath(ack.log).parent != PurePosixPath(plan.home) / "logs":
        return HopDispatchOutcome(
            machine=plan.machine, ok=False, detail="hop log is not inside the unit's logs"
        )
    return HopDispatchOutcome(
        machine=plan.machine, ok=True, detail=f"hop session {ack.session} started"
    )


async def _dispatch_hops_async(
    plans: Sequence[HopUnitPlan], timeout_s: float | None
) -> list[HopDispatchOutcome]:
    # Lazy import (the `_update_fanout` discipline): cli/commands modules load
    # on every `ava` invocation; the ops RPC stack should not.
    from ops import cluster_rpc

    async def one(plan: HopUnitPlan) -> HopDispatchOutcome:
        try:
            result = await cluster_rpc.dispatch_to_machine(
                target_machine=plan.machine,
                kind="cluster_bootstrap_hop",
                payload={
                    "hop_request": plan.request_path,
                    "artifact_digest": plan.artifact_digest,
                },
                timeout_s=timeout_s,
                ops_url=plan.ops_url,
            )
        except cluster_rpc.ClusterOpUnreachable as exc:
            return HopDispatchOutcome(machine=plan.machine, ok=False, detail=f"unreachable: {exc}")
        except cluster_rpc.ClusterOpFailed as exc:
            return HopDispatchOutcome(
                machine=plan.machine, ok=False, detail=f"op failed: {exc.result!r}"
            )
        return _hop_outcome(plan, result)

    return list(await asyncio.gather(*(one(plan) for plan in plans)))


def dispatch_bootstrap_hops(
    plans: Sequence[HopUnitPlan], *, timeout_s: float | None = None
) -> list[HopDispatchOutcome]:
    """Fan the per-unit hop plans out and print every answer (C-2).

    A unit answers by acknowledgement, not by exception: an unreachable host
    and a refused op are answers too (non-ok outcomes), so the gate prints the
    whole roster's verdict at once. Nothing is stopped, started or resumed on
    the coordinator's side; each unit's own entry owns its effects, and a unit
    that never received its request is exactly the no-effect state the
    pre-stop abort proves.
    """
    outcomes = asyncio.run(_dispatch_hops_async(plans, timeout_s))
    for outcome in outcomes:
        if outcome.ok:
            print(f"  \u2713 {outcome.machine}: {outcome.detail}")
        else:
            print(f"  \u2717 {outcome.machine}: {outcome.detail}", file=sys.stderr)
    return outcomes


def phase_b_hops(
    plans: Sequence[HopUnitPlan],
) -> tuple[int, RolloutOutcome, list[tuple[str, str | None]], str | None]:
    """The hop gate: dispatch the whole roster; CLEAN only on full acknowledgement.

    Returns `(rc, outcome, hosts_to_resume, failing_step)` in the shape the
    legacy Phase-B poll returns, except `hosts_to_resume` is a frozen empty
    list: this slice adds no hop-aware resume semantics -- after the hop gate
    the fallback is checked recovery (`ava cluster recover-pending`), and
    waiting / ledger-driven continuation are task #4129 I5/I6's.
    """
    outcomes = dispatch_bootstrap_hops(plans)
    refused = sum(1 for outcome in outcomes if not outcome.ok)
    if refused:
        return (
            1,
            RolloutOutcome.INCOMPLETE,
            [],
            f"the hop gate refused: {refused} of {len(plans)} units did not acknowledge",
        )
    return 0, RolloutOutcome.CLEAN, [], None


# The daemon's wire carries `{"error": "<class>: <message>"}` for an op that
# raised, and `ClusterUpdateInProgress` deliberately has no `reason` key (it is
# a dispatch-level verdict, not a wire-error enum), so the class-name prefix is
# the wire's only anchor for the one retryable refusal. A refused spawn
# started nothing, so re-sending the dispatch is safe; the server dedupes
# nothing on replay because nothing ran.
_IN_FLIGHT_ERROR_PREFIX = "ClusterUpdateInProgress:"


@dataclass(frozen=True)
class ContinueDispatchOutcome:
    """One unit's answer to a continuation dispatch."""

    machine: str
    ok: bool
    detail: str
    retryable: bool = False


def _is_in_flight_refusal(result: Mapping[str, object]) -> bool:
    error = result.get("error")
    return isinstance(error, str) and error.startswith(_IN_FLIGHT_ERROR_PREFIX)


def _continue_ack(
    unit: ContinueUnitInput, result: dict[str, object], *, label: str
) -> ContinueDispatchOutcome:
    """One acknowledgement, re-derived from the wire values; never trusted as such."""
    ack = NormalContinueResult.model_validate_json(json.dumps(result))
    expected_session = session_name(_UPDATER_SERVICE)
    if ack.machine != unit.machine:
        return ContinueDispatchOutcome(
            machine=unit.machine, ok=False, detail="acknowledgement belongs to another machine"
        )
    if ack.home != unit.home:
        return ContinueDispatchOutcome(
            machine=unit.machine, ok=False, detail="acknowledgement belongs to another unit home"
        )
    if ack.session != expected_session:
        return ContinueDispatchOutcome(
            machine=unit.machine,
            ok=False,
            detail=f"spawned {ack.session!r}, not the updater session",
        )
    if PurePosixPath(ack.log).parent != PurePosixPath(unit.home) / "logs":
        return ContinueDispatchOutcome(
            machine=unit.machine,
            ok=False,
            detail=f"the {label} log is not inside the unit's logs",
        )
    return ContinueDispatchOutcome(
        machine=unit.machine, ok=True, detail=f"{label} session {ack.session} started"
    )


async def _dispatch_continues_async(
    units: Sequence[ContinueUnitInput], *, step: str, label: str
) -> list[ContinueDispatchOutcome]:
    # Lazy import (the `_update_fanout` discipline): cli/commands modules load
    # on every `ava` invocation; the ops RPC stack should not.
    from ops import cluster_rpc

    async def one(unit: ContinueUnitInput) -> ContinueDispatchOutcome:
        try:
            result = await cluster_rpc.dispatch_to_machine(
                target_machine=unit.machine,
                kind="cluster_normal_continue",
                payload={
                    "continue_request": unit.request_path,
                    "step": step,
                    "artifact_digest": unit.artifact_digest,
                },
                timeout_s=None,
                ops_url=unit.ops_url,
            )
        except cluster_rpc.ClusterOpUnreachable as exc:
            return ContinueDispatchOutcome(
                machine=unit.machine, ok=False, detail=f"unreachable: {exc}"
            )
        except cluster_rpc.ClusterOpFailed as exc:
            if _is_in_flight_refusal(exc.result):
                return ContinueDispatchOutcome(
                    machine=unit.machine,
                    ok=False,
                    detail="an updater session is still in flight",
                    retryable=True,
                )
            return ContinueDispatchOutcome(
                machine=unit.machine, ok=False, detail=f"op failed: {exc.result!r}"
            )
        return _continue_ack(unit, result, label=label)

    return list(await asyncio.gather(*(one(unit) for unit in units)))


def _dispatch_step(
    phase_input: ManagedWriterPhaseInput,
    units: Sequence[ContinueUnitInput],
    *,
    step: str,
    label: str,
) -> int:
    """Fan one continuation step out and print every answer; 0 only on the full roster.

    A unit answers by acknowledgement, not by exception: an unreachable host
    and a refused op are answers too, so a deterministic refusal prints the
    roster's verdict and fails at once. The single retryable answer is the
    daemon's in-flight refusal (`ClusterUpdateInProgress`): the session that
    handed the unit over may still be exiting in the milliseconds window around
    its last journal write, and a refused spawn started nothing -- so that
    unit's dispatch is re-sent at the poll cadence until its session clears,
    bounded by the sealed window V. Nothing is stopped or started on the
    coordinator's side; each unit's own entry owns its effects.
    """
    from shared.config import settings

    deadline = phase_input.collector.valid_until
    poll_seconds = settings.gateway.update_managed_writer_hop_poll_seconds
    pending: list[ContinueUnitInput] = list(units)
    noted: set[str] = set()
    while True:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            print(
                f"\n\u2717 the {label} dispatch hit the sealed window with units not "
                f"dispatched: {', '.join(unit.machine for unit in pending)}",
                file=sys.stderr,
            )
            return 1
        outcomes = asyncio.run(_dispatch_continues_async(pending, step=step, label=label))
        retry: list[ContinueUnitInput] = []
        refused = False
        for unit, outcome in zip(pending, outcomes, strict=True):
            if outcome.ok:
                print(f"  \u2713 {unit.machine}: {outcome.detail}")
            elif outcome.retryable:
                if unit.machine not in noted:
                    noted.add(unit.machine)
                    print(
                        f"  \u00b7 {unit.machine}: an updater session is still in flight; "
                        "retrying until the sealed window closes"
                    )
                retry.append(unit)
            else:
                # Print every answer before the step fails: the operator sees the
                # whole roster's verdict at once (the hop gate's discipline).
                print(f"  \u2717 {unit.machine}: {outcome.detail}", file=sys.stderr)
                refused = True
        if refused:
            return 1
        if not retry:
            return 0
        pending = retry
        time.sleep(min(poll_seconds, max(0.0, (deadline - datetime.now(UTC)).total_seconds())))


def _wait_readbacks(
    phase_input: ManagedWriterPhaseInput, units: Sequence[ContinueUnitInput]
) -> int:
    """Wait until the pending journal reports the FULL roster's readbacks; 0 or 1.

    The database read is the journal's own checked entry (`_pending` +
    migration receipt + fresh readback validation), so its not-ready state is
    exactly `ManagedWriterBarrierError` -- a migration receipt not yet
    registered, or a readback set still forming. That state is waited out at
    the configured poll cadence, never treated as a failure; any other database
    error propagates. The sealed window V bounds the wait: past it the sealed
    evidence is stale and the rollout fails into checked recovery rather than
    sliding the window.
    """
    from shared.config import settings
    from shared.db import connect as db_connect
    from shared.managed_writer_activation import read_pending_unit_readbacks

    collector = phase_input.collector
    expected = sorted((unit.machine, unit.home) for unit in units)
    poll_seconds = settings.gateway.update_managed_writer_hop_poll_seconds
    recorded_count: int | None = None
    printed: int | None = None
    while True:
        readbacks = None
        try:
            with db_connect(autocommit=True) as conn:
                readbacks = read_pending_unit_readbacks(
                    conn, collector.operation, collector.challenge
                )
        except ManagedWriterBarrierError:
            readbacks = None
        if readbacks is not None:
            recorded_count = len(readbacks)
            if recorded_count != printed:
                printed = recorded_count
                print(
                    f"  \u00b7 managed-writer continuation: {recorded_count} of "
                    f"{len(expected)} units read back"
                )
            keys = sorted(
                (item.selector.unit.machine, item.selector.unit.home) for item in readbacks
            )
            if keys == expected:
                print(f"  \u2713 managed-writer continuation: {len(expected)} units read back")
                return 0
        remaining = (collector.valid_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            recorded = 0 if recorded_count is None else recorded_count
            print(
                f"\n\u2717 the continuation wait hit the sealed window with units not "
                f"read back: {recorded} of {len(expected)} recorded",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_seconds, remaining))


@dataclass(frozen=True)
class _TailRead:
    """One unit's recovery-slot answer, classified for the commit-tail wait."""

    machine: str
    done: bool
    detail: str
    failed: bool = False


def _tail_read(unit: ContinueUnitInput, result: dict[str, object]) -> _TailRead:
    """One recovery read, re-derived from the wire values; never trusted as such.

    Success is `normal_release.stage == "committed"` OR an absent journal. The
    absence argument is the clear gate's own: `updater_handoff.clear` runs its
    compare-and-set through `_bootstrap_clearable_unlocked`, which passes only
    once the nested normal release reads `committed` -- so a slot that is
    absent can only be a committed tail that has already disposed. A journal
    that reads `recovered` with no nested continuation records the hop's
    predecessor-recovery outcome: nothing will ever commit there, so the wait
    fails early instead of burning the window.
    """
    read = BootstrapRecoveryReadResult.model_validate_json(json.dumps(result))
    if read.machine != unit.machine:
        return _TailRead(
            machine=unit.machine,
            done=False,
            failed=True,
            detail="the recovery read answered for another machine",
        )
    if read.home != unit.home:
        return _TailRead(
            machine=unit.machine,
            done=False,
            failed=True,
            detail="the recovery read answered for another unit home",
        )
    if not read.journal_present:
        return _TailRead(
            machine=unit.machine,
            done=True,
            detail="journal absent; the committed tail has been disposed",
        )
    if read.normal_release_stage == "committed":
        return _TailRead(machine=unit.machine, done=True, detail="normal release committed")
    if read.normal_release_stage is None and read.journal_stage == "recovered":
        return _TailRead(
            machine=unit.machine,
            done=False,
            failed=True,
            detail=(
                f"the commit-tail wait: {unit.machine} recovered its predecessor instead "
                "of committing its normal release"
            ),
        )
    state = (
        f"normal release stage={read.normal_release_stage}"
        if read.normal_release_stage is not None
        else (
            f"bootstrap stage={read.journal_stage}"
            if read.journal_stage is not None
            else "the recovery journal is present but has no readable stage"
        )
    )
    return _TailRead(machine=unit.machine, done=False, detail=state)


async def _read_tails_async(
    units: Sequence[ContinueUnitInput], *, timeout_s: float
) -> list[_TailRead]:
    from ops import cluster_rpc

    async def one(unit: ContinueUnitInput) -> _TailRead:
        try:
            result = await cluster_rpc.dispatch_to_machine(
                target_machine=unit.machine,
                kind="cluster_bootstrap_recovery_read",
                payload={},
                timeout_s=timeout_s,
                ops_url=unit.ops_url,
            )
        except cluster_rpc.ClusterOpUnreachable as exc:
            return _TailRead(machine=unit.machine, done=False, detail=f"unreachable: {exc}")
        except cluster_rpc.ClusterOpFailed as exc:
            return _TailRead(machine=unit.machine, done=False, detail=f"op failed: {exc.result!r}")
        return _tail_read(unit, result)

    return list(await asyncio.gather(*(one(unit) for unit in units)))


def _wait_committed_tails(
    phase_input: ManagedWriterPhaseInput, units: Sequence[ContinueUnitInput]
) -> int:
    """Wait until every unit's recovery slot reads committed (or is absent); 0 or 1.

    A unit that cannot answer yet (unreachable, mid-write, still `observed`)
    stays pending -- with its last state printed once per change -- until the
    sealed window V closes the wait into the deadline detail. A refused or
    foreign answer and the recovered-predecessor state are deterministic and
    fail at once.
    """
    from shared.config import settings

    collector = phase_input.collector
    poll_seconds = settings.gateway.update_managed_writer_hop_poll_seconds
    timeout_s = settings.gateway.update_managed_writer_pull_timeout_seconds
    pending: list[ContinueUnitInput] = list(units)
    states: dict[str, str] = {}
    while True:
        reads = asyncio.run(_read_tails_async(pending, timeout_s=timeout_s))
        still: list[ContinueUnitInput] = []
        for unit, read in zip(pending, reads, strict=True):
            if read.done:
                print(f"  \u2713 {unit.machine}: {read.detail}")
            elif read.failed:
                print(f"\n\u2717 {read.detail}", file=sys.stderr)
                return 1
            else:
                if states.get(unit.machine) != read.detail:
                    states[unit.machine] = read.detail
                    print(f"  \u00b7 managed-writer tails: {unit.machine} {read.detail}")
                still.append(unit)
        pending = still
        if not pending:
            print(f"  \u2713 managed-writer tails: all {len(units)} units committed")
            return 0
        remaining = (collector.valid_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            pairs = ", ".join(
                f"{unit.machine}={states.get(unit.machine, 'not observed')}" for unit in units
            )
            print(
                f"\n\u2717 the commit-tail wait hit the sealed window with units not "
                f"committed: {pairs}",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_seconds, remaining))


def drive_continuation(phase_input: ManagedWriterPhaseInput) -> int:
    """Run every unit's normal-release drive, then wait for the full readback roster.

    Called by the wiring under an `active` decision, after the collection was
    adopted and before the P5 commit: dispatch the drive to every unit (with
    the in-flight refusal re-sent until its session clears), then wait on the
    pending journal until every unit's readback is recorded. Returns 0 only on
    the full roster; a refusal or a wait that hits the sealed window returns 1
    with the pending journal retained for checked recovery. An empty roster is
    a silent 0: a rollout whose begin sealed no continuation units has nothing
    to drive.
    """
    units = phase_input.continue_units
    if not units:
        return 0
    rc = _dispatch_step(phase_input, units, step="drive", label="continuation")
    if rc != 0:
        return rc
    return _wait_readbacks(phase_input, units)


def commit_tails(phase_input: ManagedWriterPhaseInput) -> int:
    """Run every unit's post-publication commit tail, then wait for its slot.

    Called by the wiring under an `active` decision, after the P5 commit was
    paid: dispatch the tail to every unit and wait until each unit's recovery
    slot reads `committed` or has been disposed. The tail re-runs idempotently
    -- it refuses unless the exact current publication covers the unit and
    returns unchanged at the committed stage -- so a wait that hits the sealed
    window fails the rollout with the commit already paid, and the re-dispatch
    is the recovery. An empty roster is a silent 0.
    """
    units = phase_input.continue_units
    if not units:
        return 0
    rc = _dispatch_step(phase_input, units, step="commit", label="commit-tail")
    if rc != 0:
        return rc
    return _wait_committed_tails(phase_input, units)
