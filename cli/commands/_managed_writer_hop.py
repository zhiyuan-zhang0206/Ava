"""The coordinator-side hop phase: per-unit plans, fan-out, and its gate (task #4129, channel C).

The hop phase starts every unit's restricted updater hop. The begin chain
assembles one `HopUnitPlan` per unit -- its private projections, their
content-named paths, and the retained candidate image whose interpreter runs
the hop (`_managed_writer_dispatch.assemble_hop_plans`); this module fans the
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
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from cli.commands._update_recover import RolloutOutcome
from ops.cluster_session import _UPDATER_SERVICE
from ops.rpc_bootstrap_hop import BootstrapHopResult
from ops.rpc_prepare_dispatch import ProjectionFile
from shared.cluster.derive import session_name
from shared.managed_writer_barrier import Digest, RolloutIdentity


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
    shipped restricted-A context's exact bytes. `prepared_receipt_digest` is the
    sealed receipt digest the collection's adoption gate binds.
    """

    machine: str
    home: str
    ops_url: str | None
    candidate_context: bytes
    request: bytes
    recovery_context: bytes
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
class ManagedWriterPhaseInput:
    """The begin chain's full hand-off: the hop plans plus the collector input.

    One container so the eagerly-loaded wiring / verdict positions carry the hop
    phase and the collection phase as a single value; the collector module
    itself stays behind method-local imports.
    """

    hop_plans: tuple[HopUnitPlan, ...]
    collector: CollectorInput


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
