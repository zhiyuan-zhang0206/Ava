"""Identity-less terminated rows at the database-records repair: agents
terminated before the runtime incarnation existed carry NULL resources and no
complete hosted runtime identity, so resurrection refuses them. The survey
counts them per machine and category; `--execute` mints a hosted identity for
each row of an attested machine, which resurrection accepts and clears, and
leaves every other row alone."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime
from ops import agent_wake
from ops.agent_spawn import create_agent_row
from scripts import cutover_db_records as records
from scripts.cutover_db_survey import Inputs
from shared.agents import ResurrectRefused
from shared.db import insert_inbound_message
from tests.lifecycle.cutover.test_db_records import (  # noqa: F401 -- fixtures
    GATEWAY,
    GONE,
    PAUSED,
    Cluster,
    _application_owner,
    _attest,
    _inputs,
    _patched_session,
    _run,
    _state,
    _survey,
    cluster,
)

UNREGISTERED = "corp-box"
_ROW = (
    "SELECT status, runtime_kind, runtime_generation::text, runtime_owner::text, pid, "
    "lifecycle_command_id, incarnation_resources FROM agents_meta WHERE id=%s"
)


def _terminated(
    db: psycopg.Connection,
    machine: str,
    *,
    kind: str | None = None,
    pid: int | None = None,
    pointer: str | None = None,
) -> int:
    """A row terminated before the runtime incarnation: NULL resources and, unless
    `kind` is hosted, no complete hosted identity. `pointer` 'pending' holds an
    unapplied restart (resurrection supersedes it), 'applied' an applied terminate."""
    aid, _, _, _ = create_agent_row(spawner="user", machine=machine)
    runtime = (uuid4(), uuid4()) if kind else (None, None)
    command = None
    if pointer == "pending":
        command = insert_inbound_message(db, aid, "", "user", kind="restart")
    elif pointer == "applied":
        found = db.execute(
            "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at,"
            "applied_at,target_generation,target_owner) "
            "VALUES(%s,'terminate','user','','claimed',now(),now(),%s,%s) RETURNING id",
            (aid, uuid4(), uuid4()),
        ).fetchone()
        assert found is not None
        command = found[0]
    db.execute(
        "UPDATE agents_meta SET status='terminated', termination_source='user', "
        "runtime_kind=%s, runtime_generation=%s, runtime_owner=%s, pid=%s, "
        "lifecycle_command_id=%s, incarnation_resources=NULL WHERE id=%s",
        (kind, *runtime, pid, command, aid),
    )
    db.commit()
    return aid


def _row(db: psycopg.Connection, aid: int) -> tuple[Any, ...]:
    row = db.execute(_ROW, (aid,)).fetchone()
    db.commit()
    assert row is not None
    return tuple(row)


def _only(inputs: Inputs, machine: str) -> Inputs:
    """The same inputs with only `machine`'s attestation."""
    return replace(
        inputs,
        attestations={machine: inputs.attestations[machine]},
        raw={machine: inputs.raw[machine]},
    )


def _categories(inputs: Inputs) -> dict[int, str]:
    return {item.agent_id: item.category for item in _survey(inputs).identityless}


@pytest.mark.parametrize(
    ("machine", "category"),
    [
        ("runner", "convertible"),
        (GATEWAY, "awaiting"),
        (GONE, "no_unit"),
        (UNREGISTERED, "no_unit"),
        (PAUSED, "paused"),
    ],
)
def test_a_row_is_classified_by_its_machines_evidence(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path, machine: str, category: str
) -> None:
    """Attested machine: convertible. A unit but no attestation: awaiting. No
    unit left (the run retires the last one, or none is registered): no_unit."""
    home = cluster.runner if machine == "runner" else machine
    aid = _terminated(db_conn, home)
    found = _survey(_only(_inputs(tmp_path, cluster), cluster.runner))
    assert {item.agent_id: item.category for item in found.identityless} == {aid: category}
    assert found.checks["D-8"]["identityless"] == {home: {category: 1}}


def test_only_identityless_terminated_rows_are_judged_and_a_blocking_pointer_is_fenced(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    runner = cluster.runner
    judged = {
        _terminated(db_conn, runner): "convertible",
        _terminated(db_conn, runner, kind="process", pid=4545): "convertible",
        _terminated(db_conn, runner, pointer="pending"): "convertible",
        _terminated(db_conn, runner, pointer="applied"): "pointer",
    }
    _terminated(db_conn, runner, kind="hosted")  # retains its identity: resurrection takes it
    assert _categories(_inputs(tmp_path, cluster)) == judged


def test_check_prints_each_category_per_machine(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for machine in (cluster.runner, cluster.runner, GATEWAY, GONE, UNREGISTERED):
        _terminated(db_conn, machine)
    units = tmp_path / "units.json"
    units.write_text(json.dumps([{"machine": GONE, "home": cluster.unit(GONE), "evidence": "x"}]))
    attestation = _attest(tmp_path, cluster, cluster.runner)
    capsys.readouterr()
    with _patched_session(write=False):
        args = ["--home", str(cluster.home), "--check", "--retire-units", str(units)]
        code = records.main([*args, "--attestation", str(attestation)])
    d8 = json.loads(capsys.readouterr().out)["checks"]["D-8"]
    assert code == 2
    assert d8["counts"]["identityless"] == {
        "convertible": 2,
        "awaiting": 1,
        "no_unit": 2,
        "paused": 0,
        "pointer": 0,
    }
    assert d8["identityless"] == {
        GATEWAY: {"awaiting": 1},
        GONE: {"no_unit": 1},
        UNREGISTERED: {"no_unit": 1},
        cluster.runner: {"convertible": 2},
    }


def _no_wake(_agent_id: int, _payload: str) -> None:
    """Resurrection publishes a Redis wake; these tests have no host to wake."""


async def _resurrect_and_admit(
    db: psycopg.Connection, pool: AsyncConnectionPool, aid: int, machine: str
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """The row after resurrection, then (owned by the successor, protocol,
    resources) after the successor's admission."""
    agent_wake.resurrect_agent(aid, resurrected_by="user")
    resurrected = _row(db, aid)
    successor = await admit_hosted_runtime(pool, aid, machine, uuid4(), expected_from="idling")
    assert successor is not None
    admitted = db.execute(
        "SELECT runtime_owner=%s, runtime_protocol_version, incarnation_resources "
        "FROM agents_meta WHERE id=%s",
        (successor.owner, aid),
    ).fetchone()
    db.commit()
    assert admitted is not None
    return resurrected, tuple(admitted)


async def test_the_minted_identity_resurrects_and_admission_takes_it_as_protocol_zero(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _no_wake)
    runner = cluster.runner
    agents = [
        _terminated(db_conn, runner),
        _terminated(db_conn, runner, kind="process", pid=4545),
        _terminated(db_conn, runner, pointer="pending"),
    ]
    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        agent_wake.resurrect_agent(agents[0], resurrected_by="user")
    before = {aid: _row(db_conn, aid) for aid in agents}
    inbound = "SELECT to_jsonb(i) FROM inbound_messages i WHERE agent_id=ANY(%s) ORDER BY id"
    receipts = db_conn.execute(inbound, (agents,)).fetchall()
    db_conn.commit()
    inputs = _inputs(tmp_path, cluster)

    run = _run(cluster, inputs)
    (effect,) = run["effects"]["identities"]
    assert (run["results"]["identities"], effect["machine"], effect["count"]) == (
        ["applied"],
        runner,
        3,
    )
    assert effect["attestation_sha256"] == inputs.digest(runner)
    minted = {row["agent_id"]: (row["generation"], row["owner"]) for row in effect["rows"]}
    for aid in agents:
        pointer = before[aid][5]
        assert _row(db_conn, aid) == ("terminated", "hosted", *minted[aid], None, pointer, None)
    # The mint writes no receipt: the agents' inbound rows are untouched.
    assert db_conn.execute(inbound, (agents,)).fetchall() == receipts
    db_conn.commit()

    # Resurrection clears the minted identity; admission takes the row as
    # protocol zero with its resources still NULL, like any idling NULL row.
    for aid in agents:
        assert await _resurrect_and_admit(db_conn, aops_pool, aid, runner) == (
            ("idling", None, None, None, None, None, None),
            (True, 0, None),
        )


def test_a_row_changed_after_planning_is_left_unchanged(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = cluster.runner
    kept, changed = _terminated(db_conn, runner), _terminated(db_conn, runner)
    real = records._write_journal

    def change_after_the_plan_is_recorded(home: Path, journal: dict[str, Any]) -> None:
        real(home, journal)
        if not any(journal["runs"][-1]["results"].values()):
            db_conn.execute("UPDATE agents_meta SET pid=999 WHERE id=%s", (changed,))
            db_conn.commit()

    monkeypatch.setattr(records, "_write_journal", change_after_the_plan_is_recorded)
    run = _run(cluster, _inputs(tmp_path, cluster))
    assert run["results"]["identities"] == [
        f"applied: minted 1, left 1 changed row(s) unchanged (agents {changed})"
    ]
    assert _row(db_conn, changed)[1:5] == (None, None, None, 999)
    assert _row(db_conn, kept)[1] == "hosted"


def test_rows_without_their_machines_attestation_stay_unchanged(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    runner = cluster.runner
    fenced = [
        _terminated(db_conn, GONE),
        _terminated(db_conn, UNREGISTERED),
        _terminated(db_conn, PAUSED),
        _terminated(db_conn, runner, pointer="applied"),
    ]
    untouched = {aid: _row(db_conn, aid) for aid in fenced}
    _run(cluster, _inputs(tmp_path, cluster))
    fresh = _inputs(tmp_path, cluster, pending=None, lease=None, retire_units=())
    d8 = _survey(fresh).checks["D-8"]
    no_unit = "terminated with no runtime identity; no unit of its machine remains to attest"
    assert d8["verdict"] == "fenced"
    assert [g["count"] for g in d8["fenced"] if g["reason"].startswith(no_unit)] == [2]
    # Cluster-wide repairs are done; a later run may attest one machine only.
    awaiting, attested = _terminated(db_conn, GATEWAY), _terminated(db_conn, runner)
    untouched[awaiting] = _row(db_conn, awaiting)
    run = _run(cluster, _only(fresh, runner))
    assert [row["agent_id"] for row in run["effects"]["identities"][0]["rows"]] == [attested]
    assert {aid: _row(db_conn, aid) for aid in untouched} == untouched
    assert _row(db_conn, attested)[1] == "hosted"
    assert _survey(_only(fresh, runner)).checks["D-8"]["verdict"] == "repair"  # still awaiting


def test_the_mint_happens_once(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the mint's commit and its journal write continues as
    `already`; the same inputs change nothing; a later run mints nothing again."""
    aid = _terminated(db_conn, cluster.runner)
    inputs = _inputs(tmp_path, cluster)
    real = records._write_journal

    def crash_after_the_mint(home: Path, journal: dict[str, Any]) -> None:
        if journal["runs"][-1]["results"]["identities"]:
            raise OSError("simulated crash between the commit and the journal write")
        real(home, journal)

    monkeypatch.setattr(records, "_write_journal", crash_after_the_mint)
    with pytest.raises(OSError, match="simulated crash"):
        _run(cluster, inputs)
    monkeypatch.setattr(records, "_write_journal", real)
    minted = _row(db_conn, aid)
    run = _run(cluster, inputs)
    assert run["results"]["identities"] == ["already"]
    (row,) = run["effects"]["identities"][0]["rows"]
    assert minted[1:4] == ("hosted", row["generation"], row["owner"])

    unchanged = _state(db_conn)
    assert _run(cluster, inputs) == run
    later = _run(cluster, _inputs(tmp_path, cluster, pending=None, lease=None, retire_units=()))
    assert later["effects"]["identities"] == []
    assert _state(db_conn) == unchanged
