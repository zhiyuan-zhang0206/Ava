"""The roster's read model: a machine renders from the heartbeat liveness pass's last
probe, nothing is dialed for it, and only an explicit fresh read (or a machine with no
fresh snapshot) dials."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import gateway.cluster.status as status_mod
from base.api_contracts.status import MachineStatus
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.app import app
from gateway.cluster import snapshots
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.roster_probe import IdentityMismatchLog
from gateway.cluster.snapshots import Snapshot
from ops.cluster import rpc as cluster_rpc
from tests.fixtures.gateway_config import gateway_test_client

_ROW = tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _row(name: str = "wsl", url: str | None = "http://wsl:8113") -> _ROW:
    return (name, url, ["agent-runner"], _NOW, None, None, False)


def _status(name: str = "wsl", **extra: object) -> dict[str, object]:
    return {
        "machine_name": name,
        "serve_gateway": False,
        "serve_agent_runner": True,
        "paused": False,
        "head_sha": "abc123",
        "running_sha": "def456",
        "shell_count": 3,
        **extra,
    }


def _snap(
    *,
    reachable: bool = True,
    failures: int = 0,
    status: dict[str, object] | None = None,
    age_s: float = 5.0,
    status_age_s: float | None = None,
) -> Snapshot:
    now = datetime.now(UTC)
    status_at = None if status is None else now - timedelta(seconds=status_age_s or age_s)
    return Snapshot(
        observed_at=now - timedelta(seconds=age_s),
        reachable=reachable,
        consecutive_failures=failures,
        status=status,
        status_at=status_at,
    )


@pytest.fixture
def no_dial(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if the roster dials a runner; returns the list of dialed names."""
    dialed: list[str] = []

    async def dispatch(*, target_machine: str, **_kw: object) -> dict[str, object]:
        dialed.append(target_machine)
        return _status(target_machine)

    monkeypatch.setattr(cluster_rpc, "dispatch_to_url", dispatch)
    return dialed


def _gather(
    rows: list[_ROW], snaps: dict[str, Snapshot] | None, database_gate: ProcessDbGate
) -> list[MachineStatus]:
    return asyncio.run(
        status_mod.gather_cluster_status(
            Database.from_settings(gate=database_gate),
            rows,
            "gateway",
            identity_log=IdentityMismatchLog(),
            snapshots=snaps,
            image=LOADED_IMAGE,
        )
    )


# --- the snapshot's own verdicts ----------------------------------------------


def test_a_snapshot_is_fresh_for_three_pass_intervals() -> None:
    assert _snap(age_s=5).fresh()
    assert _snap(age_s=snapshots.MAX_AGE_S - 1).fresh()
    assert not _snap(age_s=snapshots.MAX_AGE_S + 5).fresh()


def test_two_failed_passes_make_a_fresh_snapshot_known_down() -> None:
    assert not _snap(reachable=False, failures=1).known_down()
    assert _snap(reachable=False, failures=2).known_down()
    # A stale row is no evidence the host is down: the pass may simply not be running.
    assert not _snap(reachable=False, failures=9, age_s=3600).known_down()


# --- rendering from a snapshot ------------------------------------------------


def test_a_fresh_reachable_snapshot_renders_online_with_its_age(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    [machine] = _gather(
        [_row()], {"wsl": _snap(status=_status(), status_age_s=12)}, database_gate=database_gate
    )

    assert machine.online is True and machine.paused is False
    assert machine.running_sha == "def456" and machine.shell_count == 3
    assert machine.observed_at is not None
    assert 10 <= (datetime.now(UTC) - machine.observed_at).total_seconds() <= 20
    assert no_dial == []  # nothing was dialed


def test_one_dropped_probe_keeps_the_last_status_and_its_age(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    [machine] = _gather(
        [_row()],
        {"wsl": _snap(reachable=False, failures=1, status=_status(), status_age_s=70)},
        database_gate=database_gate,
    )

    assert machine.online is True and machine.running_sha == "def456"
    assert machine.observed_at is not None
    assert (datetime.now(UTC) - machine.observed_at).total_seconds() >= 65  # the older answer
    assert no_dial == []


@pytest.mark.parametrize(
    "failures, answered",
    [(2, True), (1, False)],
    ids=["two-failed-passes", "failed-and-never-answered"],
)
def test_a_machine_that_failed_its_passes_renders_offline(
    no_dial: list[str], failures: int, answered: bool, database_gate: ProcessDbGate
) -> None:
    # Built here, not at collection: a snapshot is fresh only for a few pass intervals, and a
    # long run reaches this test long after parametrization.
    snapshot = _snap(reachable=False, failures=failures, status=_status() if answered else None)
    [machine] = _gather([_row()], {"wsl": snapshot}, database_gate=database_gate)

    assert machine.online is False and machine.paused is None
    assert no_dial == []


def test_a_reachable_answer_that_is_not_a_cluster_status_is_online_unknown(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    [machine] = _gather(
        [_row()], {"wsl": _snap(reachable=True, status=None)}, database_gate=database_gate
    )

    assert machine.online is True and machine.paused is None
    assert no_dial == []


def test_a_snapshot_answered_by_another_host_renders_identity_mismatch(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    [machine] = _gather(
        [_row()], {"wsl": _snap(status=_status("somewhere-else"))}, database_gate=database_gate
    )

    assert machine.identity_mismatch is True and machine.online is False
    assert no_dial == []


# --- when the gateway dials ---------------------------------------------------


def test_a_machine_without_a_fresh_snapshot_is_dialed(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    """The degraded fallback for a heartbeat service that is not running."""
    rows = [_row("fresh-one"), _row("stale-one"), _row("unknown-one")]
    snaps = {
        "fresh-one": _snap(status=_status("fresh-one")),
        "stale-one": _snap(status=_status("stale-one"), age_s=3600),
    }

    machines = _gather(rows, snaps, database_gate=database_gate)

    assert sorted(no_dial) == ["stale-one", "unknown-one"]
    assert {m.name for m in machines} == {"fresh-one", "stale-one", "unknown-one"}


def test_a_fresh_read_dials_every_runner_whatever_the_snapshot_says(
    no_dial: list[str], database_gate: ProcessDbGate
) -> None:
    machines = _gather([_row("a"), _row("b")], None, database_gate=database_gate)

    assert sorted(no_dial) == ["a", "b"]
    assert all(m.online and m.observed_at is None for m in machines)  # live, no age


def test_the_gateway_remembers_no_failure_between_reads(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A host that fails a dial is dialed again by the next fresh read: there is no
    failure memory and no recovery thread in the gateway."""
    calls: list[str] = []

    async def dispatch(*, target_machine: str, **_kw: object) -> dict[str, object]:
        calls.append(target_machine)
        raise cluster_rpc.ClusterOpUnreachable("blackhole")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_url", dispatch)

    first = _gather([_row()], None, database_gate=database_gate)
    second = _gather([_row()], None, database_gate=database_gate)

    assert [m.online for m in first + second] == [False, False]
    assert calls == ["wsl", "wsl"]


def test_a_fresh_read_of_blackhole_hosts_is_bounded_by_one_dial_budget(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """The dials run in parallel under one per-dial deadline, so five hosts that
    never answer cost about one budget, not five."""
    budget_s = 0.3
    monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", budget_s)

    async def dispatch(**_kw: object) -> dict[str, object]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_url", dispatch)

    started = time.monotonic()
    machines = _gather([_row(f"h{i}") for i in range(5)], None, database_gate=database_gate)
    elapsed = time.monotonic() - started

    assert [m.online for m in machines] == [False] * 5
    assert elapsed < budget_s * 3


# --- the endpoints ------------------------------------------------------------


def _machine(conn: psycopg.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO machines (name, role, gateway_url) VALUES (%s, ARRAY['agent-runner'], %s)",
        (name, f"http://{name}:8113"),
    )
    conn.commit()


def _snapshot_row(conn: psycopg.Connection, name: str, *, running_sha: str) -> None:
    conn.execute(
        "INSERT INTO machine_status_snapshot "
        "(machine_name, observed_at, reachable, consecutive_failures, status, status_at) "
        "VALUES (%s, now(), true, 0, %s, now())",
        (name, Jsonb(_status(name, running_sha=running_sha))),
    )
    conn.commit()


def test_the_roster_endpoint_reads_the_snapshot_and_fresh_dials(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_conn.execute("TRUNCATE machines")
    _machine(db_conn, "wsl-snap")
    _snapshot_row(db_conn, "wsl-snap", running_sha="from-the-snapshot")
    dialed: list[str] = []

    async def dispatch(*, target_machine: str, **_kw: Any) -> dict[str, object]:
        dialed.append(target_machine)
        return _status(target_machine, running_sha="from-a-dial")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_url", dispatch)
    with gateway_test_client(app) as client:
        default = client.get("/api/cluster/roster").json()
        assert dialed == []
        fresh = client.get("/api/cluster/roster?fresh=true").json()

    row = next(m for m in default if m["name"] == "wsl-snap")
    assert row["running_sha"] == "from-the-snapshot" and row["observed_at"] is not None
    live = next(m for m in fresh if m["name"] == "wsl-snap")
    assert live["running_sha"] == "from-a-dial" and live["observed_at"] is None
    assert dialed == ["wsl-snap"]


def test_the_machines_endpoint_follows_the_same_two_modes(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_conn.execute("TRUNCATE machines")
    _machine(db_conn, "wsl-snap")
    _snapshot_row(db_conn, "wsl-snap", running_sha="x")
    dialed: list[str] = []

    async def dispatch(*, target_machine: str, **_kw: Any) -> dict[str, object]:
        dialed.append(target_machine)
        return _status(target_machine)

    monkeypatch.setattr(cluster_rpc, "dispatch_to_url", dispatch)
    with gateway_test_client(app) as client:
        assert client.get("/api/cluster/machines").json()[0]["live"] is True
        assert dialed == []
        assert client.get("/api/cluster/machines?fresh=true").json()[0]["live"] is True
    assert dialed == ["wsl-snap"]
