"""An in-process fleet: the coordinator and one remote unit's follower over a real channel.

The coordinator, its journal, the listener, the client and the unit journal
and follower are real; the gateway unit's release effects are
`fakes.OffDutyGateway` stand-ins (with synthetic fence and issue receipts),
the unit's effects are recorded stand-ins, and "dispatch" (the ops
`release_image_exec` submit on the unit) creates the unit journal and runs
its follower in a thread. Real clocks: the unit's report times are evidence
the coordinator judges, so both sides must share one time base; bounds are a
few seconds.

The remote-unit gate (`inventory.require_topology`, slice dbgen-8) is not in
this path: the stand-in gateway's preflight admits the fleet, which is what
dbgen-8 will do for real.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cli.release_fleet.client import CoordinatorClient
from cli.release_fleet.coordinator import Coordinator
from cli.release_fleet.follower import Follower
from cli.release_fleet.policy import FleetPolicy, UnitKey
from cli.release_fleet.progress import Instruction
from cli.release_fleet.request import CoordinatorEndpoint, FleetRequest, UnitRequest, UnitSpec
from cli.release_fleet.units import RemoteUnits
from cli.release_transition.journal import (
    Journal,
    Operation,
    create,
    exclusive,
    read_operation,
    read_request,
)
from cli.release_transition.request import ReleaseRef
from shared.cluster.authority.unit import UnitIdentity, ensure_enrollment
from shared.maintenance_state import MaintenanceHold
from tests.lifecycle.release_fleet.fakes import OffDutyGateway
from tests.lifecycle.transition.phases import journal_fence, journal_issue

RUNNER_MACHINE = "macbook-air"  # the unit home is a per-test temp dir
GATEWAY_AGENT = 7
RUNNER_AGENT = 11
POLICY = FleetPolicy(drain_s=5, close_s=1, cancel_grace_s=1, start_s=5, watch_s=1)
# The unit's poll is 0.02 s here; a continuation waits this long for re-answers.
REANSWER_S = 1.0


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception compensation."""


def _ref(artifact: str, commit: str) -> ReleaseRef:
    return ReleaseRef(
        artifact_digest=artifact * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit=commit * 40,
    )


def _point(home: Path, reference: ReleaseRef) -> None:
    (home / "releases").mkdir(parents=True, exist_ok=True)
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": reference.artifact_digest,
                "manifest_digest": reference.manifest_digest,
            }
        )
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def fleet_request(root: Path, *, excluded_only: bool = False, **policy: Any) -> FleetRequest:
    """A gateway home plus one remote unit home, each on its previous release."""
    gateway, runner = root / "gateway", root / "runner"
    for home in (gateway, runner):
        home.mkdir(mode=0o700)
    unit = UnitKey(machine=RUNNER_MACHINE, home=str(runner))
    spec = UnitSpec(
        unit=unit,
        registry=str(root / "runner-clusters.json"),
        roles=("agent-runner",),
        adapter="darwin-launchd-v1",
        previous=_ref("1", "d"),
        candidate=_ref("2", "9"),
        configuration_digest="f" * 64,
        sql_inventory_digest="0" * 64,
        receipt_digest="7" * 64,
        enrollment_id=ensure_enrollment(gateway, _identity(unit)).enrollment_id,
    )
    _point(gateway, _ref("a", "d"))
    _point(runner, spec.previous)
    fields: dict[str, Any] = {
        "id": uuid4(),
        "home": str(gateway),
        "registry": str(root / "clusters.json"),
        "created_at": datetime.now(UTC),
        "machine": "ubuntu",
        "previous": _ref("a", "d"),
        "candidate": _ref("e", "9"),
        "executor": _ref("e", "9"),
        "configuration_digest": "f" * 64,
        "policy": POLICY.model_copy(update=policy),
    }
    if excluded_only:
        from cli.release_fleet.request import Exclusion

        fields["excluded"] = (Exclusion(unit=unit, reason="offline", recorded_by="operator"),)
    else:
        fields["units"] = (spec,)
        fields["coordinator"] = CoordinatorEndpoint(host="127.0.0.1", port=_free_port())
    return FleetRequest.model_validate(fields)


def _identity(unit: UnitKey) -> UnitIdentity:
    return UnitIdentity(machine=unit.machine, home=unit.home)


class Gateway(OffDutyGateway):
    """The gateway unit's effects as recorded stand-ins; `fail` raises in one phase once."""

    def __init__(self, request: FleetRequest, *, fail: str = "") -> None:
        super().__init__(request)
        self.cohort_agents = (GATEWAY_AGENT,)
        self.fail = fail
        self.events: list[tuple[str, str | None]] = []

    def _effect(self, phase: str) -> None:
        operation = read_operation(self.request.path)
        self.events.append((phase, operation.direction))
        if phase == self.fail:
            self.fail = ""
            raise RuntimeError(f"injected gateway failure at {phase}")

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        self._effect("quiescing")
        return super().quiesce(operation)

    def stop(self, operation: Operation) -> None:
        self._effect("stopping")

    def fence(self, journal: Journal) -> None:
        journal_fence(journal)

    def authorize(self, journal: Journal) -> None:
        journal_issue(journal)

    def start(self, journal: Journal) -> None:
        self._effect("starting")

    def resume(self, operation: Operation) -> None:
        self._effect("resuming")

    def restore(self, journal: Journal) -> None:
        self._effect("restoring")


class UnitEffects:
    """A remote unit's release effects, recorded; `fail` raises in one phase once."""

    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.events: list[tuple[str, str | None]] = []

    def _effect(self, phase: str, operation: Operation) -> None:
        self.events.append((phase, operation.direction))
        if phase == self.fail:
            self.fail = ""
            raise RuntimeError(f"injected unit failure at {phase}")

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        self._effect("quiescing", operation)
        return MaintenanceHold(phase="drained", commands={RUNNER_AGENT: 1})

    def stop(self, operation: Operation) -> None:
        self._effect("stopping", operation)

    def select(self, operation: Operation) -> None:
        self._effect("selecting", operation)

    def start(self, journal: Journal) -> None:
        self._effect("starting", journal.operation)

    def observe(self, operation: Operation) -> None:
        self._effect("observing", operation)

    def resume(self, operation: Operation) -> None:
        self._effect("resuming", operation)

    def restore(self, journal: Journal) -> None:
        self._effect("restoring", journal.operation)


class Exchange:
    """The capability exchange dbgen-8 will provide: here, the generations asked for."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.generations: list[int | None] = []
        self.refuse = refuse

    def installed(self, home: Path) -> int | None:
        return 0

    def exchange(self, journal: Journal, instruction: Instruction) -> None:
        self.generations.append(instruction.generation)
        if self.refuse:
            self.refuse = False
            raise RuntimeError("injected capability refusal")


class Unit:
    """The remote unit's host: its dispatched executor runs the follower in a thread."""

    def __init__(self, gateway_home: Path, effects: UnitEffects, exchange: Exchange) -> None:
        self.gateway_home = gateway_home
        self.effects = effects
        self.exchange = exchange
        self.request: UnitRequest | None = None
        self.thread: threading.Thread | None = None
        self.errors: list[BaseException] = []
        self.silent = False
        self.deaths: Callable[[], None] | None = None

    def run(self, spec: UnitSpec, entry: str, request: bytes) -> dict[str, object]:
        """The ops `release_image_exec` of `entry` on this unit."""
        if entry == "preflight":
            return {"ready": True}
        parsed = read_request(request)
        assert isinstance(parsed, UnitRequest) and parsed.unit == spec.unit
        self.request = parsed
        if not self.silent and self.thread is None:
            create(parsed)
            self.start()
        return {"operation": str(parsed.path)}

    def start(self) -> None:
        self.thread = threading.Thread(target=self._follow, name="unit-executor", daemon=True)
        self.thread.start()

    def _follow(self) -> None:
        assert self.request is not None
        enrollment = ensure_enrollment(self.gateway_home, _identity(self.request.unit))
        client = CoordinatorClient(
            self.request.coordinator, self.request.id, self.request.unit, enrollment, timeout_s=2
        )
        while True:
            try:
                with exclusive(self.request.path) as journal:
                    Follower(journal, self.effects, client, self.exchange, poll_s=0.02).run()
                return
            except ControllerLost:
                continue  # the unit executor died; its continuation reconciles by digest
            except BaseException as exc:
                self.errors.append(exc)
                return

    def join(self, label: object = "") -> Operation | None:
        if self.thread is not None:
            self.thread.join(timeout=60)
            if self.thread.is_alive():
                state = None if self.request is None else read_operation(self.request.path)
                raise AssertionError(f"the unit executor did not finish {label}: {state}")
        assert not self.errors, self.errors
        return None if self.request is None else read_operation(self.request.path)


def coordinate(journal: Journal, gateway: Gateway, units: RemoteUnits) -> None:
    import time

    Coordinator(journal, gateway, units, sleep=time.sleep).run()


def units(request: FleetRequest, unit: Unit) -> RemoteUnits:
    """The coordinator's side, with a re-answer window short enough for tests."""
    return RemoteUnits(request, transport=unit, reanswer_s=REANSWER_S)


def run(request: FleetRequest, gateway: Gateway, unit: Unit) -> Operation:
    """Create the fleet journal and run the coordinator to completion (or its hold)."""
    create(request)
    with exclusive(request.path) as journal:
        coordinate(journal, gateway, units(request, unit))
    return read_operation(request.path)
