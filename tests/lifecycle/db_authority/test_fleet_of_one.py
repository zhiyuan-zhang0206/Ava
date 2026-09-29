"""A fleet of one on real PostgreSQL 17, PgBouncer and Redis.

A single-box home born by the real start steps (`test_single_box.born`) runs
the fleet coordinator from `prepared` to `complete` as its finite executor
does (`adopt_executor_authority`, then `run_coordinator`'s loop). Real: the
fleet inventory gate over `machine_units` / `machines`, the reservation and
configuration gates, the cluster deploy lease in `deployment_state`, the
write-generation fence and admission (termination census, the start's fresh
pooler), every shared-core sample (administrator `SELECT 1`, Redis `PING`,
the issued generation, `verify_active`), the cohort agent's liveness row,
alert rows through the alerts ingest, and `releases/fleet-state.json`. Only
the application root is a stand-in: no image runs here.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.commands.data_plane import bringup
from cli.commands.data_plane import pgbouncer as pooler
from cli.release_fleet.coordinator import Coordinator
from cli.release_fleet.gateway import DeployLease, GatewayUnit, read_state
from cli.release_fleet.policy import FleetPolicy
from cli.release_fleet.publication import FleetRelease
from cli.release_fleet.request import FleetRequest, fleet_release
from cli.release_fleet.units import RemoteUnits
from cli.release_transition import authority as release_authority
from cli.release_transition.journal import Journal, Operation, create, exclusive, read_operation
from cli.release_transition.request import ReleaseRef
from cli.start_identity import mark_phase
from shared import cluster
from shared.cluster import authority
from shared.deploy.maintenance.state import MaintenanceHold
from shared.deploy.release.start_inputs import configuration_digest
from tests.lifecycle.db_authority.test_release_fence import executor as executor
from tests.lifecycle.db_authority.test_single_box import Born
from tests.lifecycle.db_authority.test_single_box import born as born
from tests.lifecycle.db_authority.test_single_box import configured as configured

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_MACHINE = "test"
_AGENT = 970001
_PREVIOUS = ReleaseRef(
    artifact_digest="a" * 64,
    manifest_digest="b" * 64,
    schema_digest="c" * 64,
    source_commit="d" * 40,
)
_CANDIDATE = _PREVIOUS.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})


def _point(home: Path, reference: ReleaseRef) -> None:
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": reference.artifact_digest,
                "manifest_digest": reference.manifest_digest,
            }
        )
    )


def _live(born: Born, *, live: bool) -> None:
    lease = "now() + interval '1 hour'" if live else "now() - interval '1 second'"
    with born.admin() as conn:
        conn.execute(f"UPDATE agents_meta SET lease_expires_at = {lease} WHERE id = %s", (_AGENT,))  # noqa: S608 — one of two literal intervals


class RootlessGateway(GatewayUnit):
    """The real coordinator side of the gateway unit around a stand-in root.

    The stand-in root drains the cohort agent's runtime into the hold (as a
    real drain would), moves the selector, starts only on the issued
    generation, and keeps the agent live unless the test takes it down.
    """

    def __init__(self, request: FleetRequest, born: Born) -> None:
        self.request = request
        self.home = Path(request.home)
        self.lease = DeployLease(request.id)
        self.born = born
        self.down_on_candidate = False
        self.leases: list[str | None] = []

    def release_of(self, reference: ReleaseRef) -> FleetRelease:
        return fleet_release(reference, "0" * 64)  # the digests name no image to read

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        with self.born.admin() as conn:
            row = conn.execute("SELECT holder FROM deployment_state WHERE id = 1").fetchone()
        self.leases.append(None if row is None else row[0])
        return MaintenanceHold(phase="drained", commands={_AGENT: 1})

    def stop(self, operation: Operation) -> None:
        _live(self.born, live=False)

    def fence(self, journal: Journal) -> None:
        release_authority.fence(journal)

    def select(self, operation: Operation) -> None:
        _point(self.home, operation.reference)

    def authorize(self, journal: Journal) -> None:
        release_authority.authorize(journal, journal.operation.reference)

    def start(self, journal: Journal) -> None:
        release_authority.require_issued(journal.operation)
        # The stage's ordinary start, not the executor, serves the issued pair.
        bringup._ensure_pooler(
            self.born.record, "ava", self.home, authority.active_generation(self.home)
        )

    def observe(self, operation: Operation) -> None:
        return

    def observe_root(self, operation: Operation) -> None:
        return

    def resume(self, operation: Operation) -> None:
        _live(self.born, live=not (self.down_on_candidate and operation.direction == "candidate"))

    def restore(self, journal: Journal) -> None:
        return


@pytest.fixture
def fleet(born: Born, executor: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FleetRequest]:
    """The born home as the cluster's one registered unit, on release A, with one agent."""
    import shared.machine

    del executor
    home = born.home
    registry = Path(cluster.registry_path())
    cluster.save_record_locked(born.record, path=registry)
    mark_phase(home, "provisioned")
    mark_phase(home, "ready")
    monkeypatch.setattr(shared.machine, "machine_name", lambda: _MACHINE)
    monkeypatch.setattr(shared.machine, "machine_role", lambda: frozenset({"gateway"}))
    (home / "releases").mkdir()
    _point(home, _PREVIOUS)
    with born.admin() as conn:
        conn.execute(
            "INSERT INTO machines (name, role) VALUES (%s, '{gateway,agent-runner}')", (_MACHINE,)
        )
        conn.execute(
            "INSERT INTO machine_units (machine_name, home, serve_gateway, serve_agent_runner)"
            " VALUES (%s, %s, true, true)",
            (_MACHINE, str(home)),
        )
        conn.execute("INSERT INTO agents (id) VALUES (%s)", (_AGENT,))
        conn.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'running', %s)",
            (_AGENT, _MACHINE),
        )
    _live(born, live=True)
    yield FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(registry),
        created_at=datetime.now(UTC),
        machine=_MACHINE,
        previous=_PREVIOUS,
        candidate=_CANDIDATE,
        executor=_CANDIDATE,
        configuration_digest=configuration_digest(home),
        policy=FleetPolicy(watch_s=1, min_affected=1),
    )


def _run(request: FleetRequest, gateway: RootlessGateway) -> Operation:
    create(request)
    with exclusive(request.path) as journal:
        Coordinator(journal, gateway, RemoteUnits(request)).run()
    return read_operation(request.path)


def _alerts(born: Born, operation: str) -> list[str]:
    with born.admin() as conn:
        rows = conn.execute(
            "SELECT labels->>'event' FROM alerts WHERE source = 'release-fleet'"
            " AND labels->>'operation' = %s ORDER BY id",
            (operation,),
        ).fetchall()
    return [row[0] for row in rows]


def _lease_holder(born: Born) -> Any:
    with born.admin() as conn:
        row = conn.execute("SELECT holder FROM deployment_state WHERE id = 1").fetchone()
    return None if row is None else row[0]


def test_a_clean_fleet_of_one_publishes_its_exercised_candidate_as_known_good(
    born: Born, fleet: FleetRequest
) -> None:
    gateway = RootlessGateway(fleet, born)
    final = _run(fleet, gateway)
    assert final.fleet is not None and final.fleet.outcome == "clean", final.error
    assert [(v.stage, v.action, v.outcome) for v in final.fleet.verdicts] == [
        ("start", "proceed", None),
        ("watch", "commit", "clean"),
    ]
    cohort = final.fleet.cohort
    assert cohort is not None and cohort.size == 1
    # The lease was the operation's own while it worked, and is free after.
    assert gateway.leases == [f"fleet:{fleet.id}"]
    assert _lease_holder(born) is None
    assert authority.active_generation(born.home).number == 1
    state = read_state(born.home)
    assert state is not None and state.operation == fleet.id
    assert state.current == state.last_known_good == fleet_release(_CANDIDATE, "0" * 64)
    assert _alerts(born, str(fleet.id)) == []


def test_a_failing_watch_window_recovers_on_a_new_generation_and_alerts(
    born: Born, fleet: FleetRequest
) -> None:
    gateway = RootlessGateway(fleet, born)
    gateway.down_on_candidate = True
    final = _run(fleet, gateway)
    assert final.fleet is not None and final.fleet.outcome == "recovered", final.error
    assert final.direction == "previous"
    decision = final.fleet.decisions[0]
    assert (decision.kind, decision.phase) == ("recover", "watching")
    # Generation 0 (the predecessor's) -> 1 (the candidate's, fenced) -> 2.
    assert [(f.direction, f.generation.number) for f in final.db_fences] == [
        ("candidate", 0),
        ("previous", 1),
    ]
    assert authority.active_generation(born.home).number == 2
    state = read_state(born.home)
    assert state is not None and state.current == fleet_release(_PREVIOUS, "0" * 64)
    assert [r.release for r in state.rejected] == [fleet_release(_CANDIDATE, "0" * 64)]
    assert state.last_known_good is None
    events = _alerts(born, str(fleet.id))
    assert {"threshold_exceeded", "recovering", "recovered"} <= set(events)
    assert _lease_holder(born) is None


def test_a_second_registered_unit_aborts_before_any_effect(born: Born, fleet: FleetRequest) -> None:
    with born.admin() as conn:
        conn.execute("INSERT INTO machines (name) VALUES ('macbook-air')")
        conn.execute(
            "INSERT INTO machine_units (machine_name, home, serve_agent_runner)"
            " VALUES ('macbook-air', '/Users/zzy/.ava', true)"
        )
    gateway = RootlessGateway(fleet, born)
    final = _run(fleet, gateway)
    assert final.fleet is not None and final.fleet.outcome == "aborted"
    decision = final.fleet.decisions[0]
    assert decision.phase == "prepared" and "dbgen-8" in decision.reason
    assert gateway.leases == [] and _lease_holder(born) is None
    assert authority.active_generation(born.home).number == 0
    assert read_state(born.home) is None
