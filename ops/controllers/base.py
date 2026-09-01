"""Controller protocol + reconcile result for the ops controller-manager.

A controller owns ONE state dimension (schema version, cluster pin, cluster
pause). Each tick it observes actual state, diffs it against the Spec's desired
state, and acts to converge — with its own cooldown/backoff so a dimension that
cannot converge (an unfetchable pin, a gateway that has not migrated yet) cannot
become a hot loop. Every heal logs loudly in the controller module; the manager
records the last result per dimension so Status (``ops.observe``) can surface
"which dimension reconciled, and whether it acted".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from shared.machine import MachineRole


class BlockScope(StrEnum):
    """How much of a round a controller's reconcile holds back.

    A controller states the BLAST RADIUS of its own finding; it never names
    services. The watchdog matches that radius against what each service declares
    it needs (``ServiceSpec.requires_db``) and skips exactly the intersection — so
    a controller cannot hold back a service it knows nothing about.

    The members are ordered widest-last and nest: ``NONE`` ⊂ ``DB_DEPENDENT`` ⊂
    ``ALL``.

    - ``NONE`` — nothing is held back; the round runs its full roster.
    - ``DB_DEPENDENT`` — the cluster's Postgres is unusable *from here* (unreachable,
      or its applied migrations disagree with this checkout). Only services that
      read or write it are held back; services that never touch Postgres are
      unaffected by that reason and keep being revived.

      Holding the DB's users back — rather than reviving them and letting them fail —
      is deliberate, and not merely because a crash loop is untidy. ``unreachable``
      alone dooms the revive: ``assert_schema_current`` opens its OWN connection, so
      the boot check fails on unreachability itself, no schema drift required. And a
      doomed revive is not free — every DB-dependent healthcheck uses
      ``respawn_and_verify``, whose ``_VERIFY_DEADLINE_S`` is 20s, while a round runs
      its checks SEQUENTIALLY inside one 60s interval. Five doomed gateway daemons
      would spend 100s of a 60s round waiting on daemons that cannot come up, starving
      the DB-free services this scope exists to keep alive. Recovery is not delayed by
      waiting: both readings revive within one round of the DB returning.
    - ``ALL`` — this host is mid-transition of its own code or processes (paused by a
      rollout, an ``ava cluster update`` just spawned, an off-pin force-update). Everything
      here is about to be restarted by that transition, so reviving anything fights
      it.
    """

    NONE = "none"
    DB_DEPENDENT = "db-dependent"
    ALL = "all"


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of one controller reconcile pass.

    Attributes:
        dimension: the state dimension (the controller's ``name``), for logs + Status.
        blocks: how much of the round this pass holds back (``BlockScope``) — the
            blast radius of the finding, not a list of services. ``NONE`` lets the
            round proceed in full; ``DB_DEPENDENT`` holds back only the services that
            need Postgres; ``ALL`` holds back the whole roster. The manager
            short-circuits on the first controller that blocks anything, matching the
            watchdog's original gate ordering.
        acted: True iff this pass took a state-changing action (spawned an update,
            self-unpaused). Distinct from ``blocks``: a normally-paused host blocks
            without acting; an in-cooldown schema mismatch blocks without acting.
            Used by Status to tell "healed" from "waiting".
        detail: one-line human-readable summary for Status/logs (e.g.
            ``"off-pin abc1234 != def5678"``), or None when there is nothing to say.
    """

    dimension: str
    blocks: BlockScope
    acted: bool = False
    detail: str | None = None


@runtime_checkable
class Controller(Protocol):
    """One reconciler for one state dimension.

    ``name`` is the dimension label (stable, used as the Status key). ``reconcile``
    runs one observe→diff→act pass and returns a ``ReconcileResult``. It must be
    synchronous and self-contained — the manager offloads it to a thread so a
    blocking DB/HTTP call never freezes the event loop — and must never raise on a
    transient failure: degrade to a non-blocking result and log, so one flaky
    dimension cannot crash the tick.
    """

    name: str
    # A controller may put an upper bound on its blocking reconcile work. The
    # manager turns an expired bound into an ALL-scoped block: work launched in
    # a thread cannot be safely killed, so letting later controllers or service
    # healthchecks race its unknown outcome would be unsafe.
    timeout_s: float | None

    def reconcile(self, role: MachineRole) -> ReconcileResult: ...
