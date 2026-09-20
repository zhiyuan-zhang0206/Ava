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
recover-pending`); waiting and the ledger-driven continuation arrive with task
#4129 I5/I6.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from cli.commands._update_recover import RolloutOutcome
from ops.cluster_session import _UPDATER_SERVICE
from ops.rpc_bootstrap_hop import BootstrapHopResult
from ops.rpc_prepare_dispatch import ProjectionFile
from shared.cluster.derive import session_name
from shared.managed_writer_barrier import Digest


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
