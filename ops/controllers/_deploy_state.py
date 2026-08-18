"""Shared deploy-in-flight read layer + guard pipeline for the heal controllers.

The pin, code, and schema controllers used to inline the same two questions —
"is a cluster update lease held, and what kind of hold is it" and "is a local
updater session in flight" — in three copies that had already drifted apart:

- pin reads the lease and the orchestration inside ``try/except`` (a read failure
  defers); code read the lease defensively but let an orchestration read failure
  propagate as an exception; schema read both defensively.
- pin and code pass a settle hold that names THIS host (``DeployLease.awaits``,
  the issue #1020 exception), while schema deliberately passes every settle hold
  — its heal answers a cluster-shared condition, not this host's convergence.

One strategy change used to mean two or three edits: on 2026-07-30 the settle-hold
exception landed in ``code.py`` first, and the identical edit in ``pin.py``
followed 28 minutes later (commits ``427f47a9`` / ``5a7e9e9c``). This module is
the single copy of the *reading and classifying* half, with the one behavioral
difference (narrow vs pass) promoted from prose to a parameter. Each controller
keeps its own *response*: pin and code drive a table of guards
(``Guard`` + ``run_guards``) whose rows are one predicate each, and schema keeps
its ``(BlockScope, why)`` shape because its guard answers also decide how much of
the round is blocked — the shared part is the reading, not the responding.

All readers are conservative: unreadable evidence defers, because acting on
missing evidence is the expensive direction for every heal here (they spawn a
restart, a force-checkout, or an ``ava cluster update`` on a live host).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal

# Module-object access (not from-imports) so the tests' monkeypatch seams —
# ``shared.cluster_lock.read_update_lease`` / ``shared.machine.machine_name`` /
# ``ops.cluster.current_orchestration`` — keep working: a from-import binds the
# name at import time and a patch of the source module would no longer be seen.
import ops.cluster as _cluster
import shared.cluster_lock as _cluster_lock
import shared.machine as _machine

_log = logging.getLogger("ops.controllers.deploy_state")


class GuardVerdict(Enum):
    """One guard's answer to "may the heal proceed past me?".

    ``DEFER`` stops the pipeline (nothing is spawned), ``PROCEED`` keeps it
    going, ``PROCEED_WARN`` keeps it going while the guard says so loudly.
    """

    DEFER = "defer"
    PROCEED = "proceed"
    PROCEED_WARN = "proceed_warn"


# The three verdicts as module constants, so a guard chain reads as
# ``return Defer`` / ``return Proceed`` / ``return ProceedWarn`` and a caller
# compares ``is Defer`` / ``is Proceed``.
Defer = GuardVerdict.DEFER
Proceed = GuardVerdict.PROCEED
ProceedWarn = GuardVerdict.PROCEED_WARN


@dataclass(frozen=True)
class LeaseVerdict:
    """The cluster update lease, classified for a healer.

    ``kind`` is one of:

    - ``free`` — no live lease; the lease guard passes.
    - ``unreadable`` — the lease could not be read; conservative defer.
    - ``executing`` — a rollout is mutating the cluster right now; defer.
    - ``settle_hold`` — a stated waiting period with nobody executing under it
      (``note`` set). Whether it defers depends on ``settle_hold_mode``:
      ``narrow`` passes only when ``waits_for_this_host`` (the hold names this
      host — issue #1020), ``pass`` lets every settle hold through.

    ``holder`` is the lease holder's name for diagnostics; ``describe`` is
    ``DeployLease.describe()``, kept for the warning a narrow-mode guard prints
    when it lets a settle hold through.
    """

    kind: Literal["free", "unreadable", "executing", "settle_hold"]
    holder: str | None
    waits_for_this_host: bool | None
    describe: str | None = None


def read_lease_state(*, settle_hold_mode: Literal["narrow", "pass"]) -> LeaseVerdict:
    """Read and classify the cluster update lease.

    ``narrow`` is the pin/code semantics: only a settle hold naming this host
    passes, everything else defers (an executing lease defers too). ``pass`` is
    the schema semantics: an executing lease defers, every settle hold passes —
    that heal answers "my code is behind the DB the whole cluster shares", a
    condition no other host's settle window creates or clears, so narrowing it to
    ``awaits`` would only import the mutual wait #1020 exists to remove.

    An unreadable lease is ``unreadable`` (never an exception): every consumer
    treats that as a deferral.
    """
    try:
        lease = _cluster_lock.read_update_lease()
    except Exception:
        return LeaseVerdict(kind="unreadable", holder=None, waits_for_this_host=None)
    if lease is None:
        return LeaseVerdict(kind="free", holder=None, waits_for_this_host=None)
    if lease.note is None:
        return LeaseVerdict(kind="executing", holder=lease.holder, waits_for_this_host=None)
    if settle_hold_mode == "pass":
        return LeaseVerdict(kind="settle_hold", holder=lease.holder, waits_for_this_host=None)
    return LeaseVerdict(
        kind="settle_hold",
        holder=lease.holder,
        waits_for_this_host=lease.awaits(_machine.machine_name()),
        describe=lease.describe(),
    )


@dataclass(frozen=True)
class OrchestrationState:
    """Whether a local updater session is in flight on this host.

    ``kind`` is ``none`` (nothing running), ``in_flight`` (``name`` is the
    session's orchestration kind), or ``unreadable`` (the session read failed —
    conservative defer). This is the signal that sees the watchdog-spawned
    updater the lease is blind to: it takes no lease at all, and "off-pin" or
    "stale processes" is that updater's own mid-flight state (issue #1074).
    """

    kind: Literal["none", "in_flight", "unreadable"]
    name: str | None


def read_orchestration() -> OrchestrationState:
    """Is a local updater session in flight?

    Uniformly conservative: an unreadable answer is ``unreadable`` (defer), the
    semantics the pin controller always had. The code controller used to let the
    read exception propagate — a drift between the copies this module replaces.
    """
    try:
        name = _cluster.current_orchestration()
    except Exception:
        return OrchestrationState(kind="unreadable", name=None)
    if name is None:
        return OrchestrationState(kind="none", name=None)
    return OrchestrationState(kind="in_flight", name=name)


class GuardCtx:
    """What a guard chain reasons about: the drift's two shas.

    ``head`` is the checkout's sha; ``target`` is what the heal converges to —
    the running sha for the code controller's restart, the pin for the pin
    controller's force-checkout. The names are deliberately controller-neutral;
    each guard's log template renders them in its own words.
    """

    __slots__ = ("head", "target")

    def __init__(self, head: str, target: str) -> None:
        self.head = head
        self.target = target


class Guard:
    """One row of a guard table: a name (for tests and traces) and the predicate.

    The predicate logs its own verdict — the deferral reason and the warning are
    one thought with the decision, so they live in the same closure, capturing
    the controller's logger and log prefix. ``run_guards`` only short-circuits.
    """

    __slots__ = ("name", "verdict")

    def __init__(
        self,
        name: str,
        verdict: Callable[[GuardCtx], GuardVerdict],
    ) -> None:
        self.name = name
        self.verdict = verdict


def run_guards(guards: list[Guard], ctx: GuardCtx) -> GuardVerdict:
    """Run a guard table in order.

    The first ``Defer`` short-circuits the pipeline (the predicate already
    logged its reason); a ``ProceedWarn`` logs its warning and the pipeline
    continues; every guard passing returns ``Proceed``. Adding a guard to a
    controller is adding one row to its table — not nesting another ``if``.
    """
    for guard in guards:
        verdict = guard.verdict(ctx)
        if verdict is Defer:
            return Defer
    return Proceed
