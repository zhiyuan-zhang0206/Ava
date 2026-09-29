"""The gateway unit's part of a fleet release, run in the coordinator process.

`GatewayUnit` is the one-home effect module (`LocalTransition`) plus what only
the coordinator does on the gateway home: the cluster deploy lease, the
evidence the start barrier and the watch window judge, the fleet release
identity, known-good publication and alert delivery.

Evidence is the coordinator's own observation, never the legacy health probe:

- `gateway`, `schedules`, `delivery`: the selected root's full-roster
  readiness in its own image (`stage --observe`); the schedule manager and
  delivery run inside that roster, so one observation vouches for all three.
- `database`: an administrator `SELECT 1`; `redis`: a `PING`.
- `authorization`: the ledger's active generation is this direction's issue.
- `pooler`, `fence`: `verify_active` (the pooled logins answer, no fenced
  session survives, the catalog invariant holds). A failure fails both.
- agents: a live incarnation is an unterminated row with a live lease on its
  cohort unit's machine; a runtime error is a fatal turn since the interval
  began. Outcome-unknown quarantine is not represented anywhere yet, so no
  agent is ever reported quarantined.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

from base.log import logger
from cli.release_fleet.alerting import Delivery, deliveries
from cli.release_fleet.policy import AlertRoute, Cohort
from cli.release_fleet.progress import AlertRecord
from cli.release_fleet.publication import (
    Completion,
    FleetRelease,
    FleetState,
    publish,
    require_admissible,
)
from cli.release_fleet.request import FleetRequest, fleet_release, sql_inventory_digest
from cli.release_fleet.workload import CORE_SIGNALS, AgentReport, CoreReport, UnitReport
from cli.release_transition.journal import Operation
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import ReleaseRef

_STATE = "fleet-state.json"
_MAX_STATE_BYTES = 256 * 1024
_ROOT_SIGNALS = frozenset({"gateway", "schedules", "delivery"})


class Samples(NamedTuple):
    gateway: UnitReport
    core: tuple[CoreReport, ...]
    agents: tuple[AgentReport, ...]


def _probe(check: Callable[[], object]) -> str | None:
    """None when `check` passed; its failure otherwise. Unknown is never healthy."""
    try:
        check()
    except Exception as exc:  # a sample reports any failure
        return f"{type(exc).__name__}: {exc}"[:512]
    return None


def _database() -> None:
    from base.db import connect

    with connect() as conn:
        conn.execute("SELECT 1")


def _redis() -> None:
    from base.events.live.redis_client import sync_redis

    sync_redis().ping()  # pyright: ignore[reportUnknownMemberType] — redis-py types **kwargs as Unknown


class DeployLease:
    """The cluster deploy lease (`deployment_state`, kind `update`), dispatch to completion.

    The holder is `fleet:<operation id>`, never a local pid, so no stranded-lease
    reclaimer can judge it provably gone: a continuation re-arms it by renewing
    as the same holder, and a dead coordinator's lease lapses only by its TTL.
    A thread renews it every `LEASE_RENEW_INTERVAL_S` (the PITR activation
    renewer's cadence). A renewal that raises is a missed round, retried until
    the lease could lapse before the next one (`cluster_lock.lease_may_lapse`);
    one answered "not yours", or failures lasting that long, mark the lease
    lost, and the coordinator fails its next step on it.
    """

    def __init__(self, operation: UUID) -> None:
        self.holder = f"fleet:{operation}"
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._renewer: threading.Thread | None = None

    def hold(self) -> None:
        """Take the lease, or re-arm this operation's own after an executor restart.

        Only a live lease this operation already holds is renewed: renewing a
        free one reports it lost ("the lease is not theirs"), which is not what
        a first hold means. Both writes stay compare-and-set, so a lease taken
        between the read and the write still refuses.
        """
        from base.deploy.state.cluster_lock import (
            acquire_update_lock,
            renew_update_lock,
            update_lock_holder,
        )

        if self._renewer is not None:
            return
        rearmed = update_lock_holder() == self.holder and renew_update_lock(self.holder)
        if not (rearmed or acquire_update_lock(self.holder, kind="update")):
            raise RuntimeError(
                "the cluster deploy lease is held by another operation, or a publication is pending"
            )
        self._renewer = threading.Thread(target=self._renew, name="fleet-lease", daemon=True)
        self._renewer.start()

    def _renew(self) -> None:
        from base.deploy.progress_timeout import LEASE_RENEW_INTERVAL_S
        from base.deploy.state.cluster_lock import lease_may_lapse, renew_update_lock

        renewed = time.monotonic()
        while not self._stop.wait(LEASE_RENEW_INTERVAL_S):
            try:
                owned = renew_update_lock(self.holder)
            except Exception as exc:  # a missed round is not fatal until the lease may lapse
                logger.warning("[release-fleet] deploy lease renewal missed: {exc}", exc=exc)
                if not lease_may_lapse(time.monotonic() - renewed):
                    continue
                owned = False
            if not owned:
                self._lost.set()
                return
            renewed = time.monotonic()

    def require(self) -> None:
        if self._lost.is_set():
            raise RuntimeError("the fleet operation lost its cluster deploy lease")

    def release(self) -> None:
        """Stop renewing, then release; a lease this operation never took is left alone."""
        from base.deploy.state.cluster_lock import release_update_lock

        self._stop.set()
        if self._renewer is not None:
            self._renewer.join()
        try:
            release_update_lock(self.holder)
        except Exception:  # its TTL releases it
            logger.warning("[release-fleet] deploy lease release failed; it lapses by TTL")


class GatewayUnit(LocalTransition):
    request: FleetRequest

    def __init__(self, request: FleetRequest) -> None:
        super().__init__(request)
        self.lease = DeployLease(request.id)

    # ── preflight ───────────────────────────────────────────────────────────

    def preflight(self) -> None:
        """The one-home gates plus the fleet's own: its release history admits it."""
        super().preflight()
        state = read_state(self.home)
        if state is not None and state.current != self.release_of(self.request.previous):
            raise ValueError("the fleet's published release is not this request's predecessor")
        require_admissible(
            state,
            self.release_of(self.request.candidate),
            self.request.policy.acknowledged_rejection,
        )

    def release_of(self, reference: ReleaseRef) -> FleetRelease:
        image = self.candidate if reference == self.request.candidate else self.previous
        return fleet_release(reference, sql_inventory_digest(image))

    # ── evidence ────────────────────────────────────────────────────────────

    def sample(
        self,
        operation: Operation,
        cohort: Cohort,
        *,
        since: datetime,
        observed: datetime,
        agents: bool,
    ) -> Samples:
        from cli.release_transition.authority import require_issued, verify_active

        root = _probe(lambda: self.observe_root(operation))
        generation = _probe(lambda: verify_active(operation))
        failures = {
            "database": _probe(_database),
            "redis": _probe(_redis),
            "authorization": _probe(lambda: require_issued(operation)),
            "pooler": generation,
            "fence": generation,
        } | dict.fromkeys(_ROOT_SIGNALS, root)
        core = tuple(
            CoreReport(
                signal=signal,
                ok=failures[signal] is None,
                observed_at=observed,
                detail=failures[signal],
            )
            for signal in CORE_SIGNALS
        )
        gateway = UnitReport(
            unit=self.request.gateway,
            state="ready" if root is None else "failed",
            observed_at=observed,
            detail=root,
        )
        sampled = self._agents(cohort, since, observed) if agents else ()
        return Samples(gateway=gateway, core=core, agents=sampled)

    def _agents(
        self, cohort: Cohort, since: datetime, observed: datetime
    ) -> tuple[AgentReport, ...]:
        """One sample per cohort agent; an unreadable roster yields none (unknown)."""
        from base.db import connect

        members = cohort.members
        try:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT id, status <> 'terminated' AND lease_expires_at > now(), machine,"
                    " last_turn_fatal_at IS NOT NULL AND last_turn_fatal_at >= %s"
                    " FROM agents_meta WHERE id = ANY(%s)",
                    (since, sorted(members)),
                ).fetchall()
        except Exception:  # unknown agents are affected at the window end
            logger.warning("[release-fleet] agent evidence unreadable; agents stay unobserved")
            return ()
        return tuple(
            AgentReport(
                agent=agent_id,
                live=bool(live) and machine == members[agent_id].machine,
                runtime_error=bool(fatal),
                quarantined=False,
                observed_at=observed,
            )
            for agent_id, live, machine, fatal in rows
        )

    # ── completion ──────────────────────────────────────────────────────────

    def publish(self, completion: Completion) -> None:
        """Write `releases/fleet-state.json` once per operation; a retry is a no-op."""
        from base.host.atomic_io import write_text_atomic

        prior = read_state(self.home)
        if prior is not None and prior.operation == completion.operation:
            return
        state = publish(prior, completion)
        if state is None:
            return
        write_text_atomic(
            self.home / "releases" / _STATE,
            state.model_dump_json() + "\n",
            mode=0o600,
            sync_file=True,
            sync_parent=True,
        )

    def deliver(self, record: AlertRecord, route: AlertRoute) -> tuple[str, ...]:
        """Each route not yet landed; a failed delivery is retried at the next boundary."""
        return tuple(
            delivery.kind
            for delivery in deliveries(record.alert, route)
            if delivery.kind not in record.delivered and self._landed(delivery, record)
        )

    def _landed(self, delivery: Delivery, record: AlertRecord) -> bool:
        from cli.release_fleet.delivery import deliver_one

        try:
            deliver_one(self.home, record.alert, delivery)
        except Exception as exc:  # delivery never blocks the operation
            logger.warning(
                "[release-fleet] {kind} for {key} not delivered: {error}",
                kind=delivery.kind,
                key=record.alert.key,
                error=exc,
            )
            return False
        return True


def read_state(home: Path) -> FleetState | None:
    """The published cluster release state, or None before the first completion."""
    from base.deploy.release.verified_file import regular_bytes

    path = home / "releases" / _STATE
    try:
        encoded = regular_bytes(path, max_bytes=_MAX_STATE_BYTES)
    except FileNotFoundError:
        return None
    return FleetState.model_validate_json(encoded)
