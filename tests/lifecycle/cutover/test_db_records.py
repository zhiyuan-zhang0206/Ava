"""The one-time database-records repair (FC-4) against a throwaway PostgreSQL
shaped like the rows the retired runtime left: drained and terminated agents
whose process receipts lack boot scope, a live old incarnation, a paused
machine, a row without a receipt, protocol-zero NULL and current-model rows, a
durable pending publication, a legacy deploy-lease holder, `paused` host
postures and a unit whose home is gone."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime
from ops.agent_spawn import create_agent_row
from scripts import cutover_db_records as records
from scripts import cutover_inventory as inventory
from scripts.cutover_db_survey import FREE_LEASE, Inputs, RetiredUnit, export_rows, survey
from shared.config import settings
from shared.incarnation_resources import IncarnationResources, ResourceBirth, decode_resources
from shared.machine import machine_name

GATEWAY, PAUSED, GONE = "gw-box", "win-box", "gone-box"
_DRAIN = {"maintenance": {"holder": "legacy:pid41", "acquired_at": "2026-09-27T01:00:00+00:00"}}
_PENDING = {"operation": {"holder": "ubuntu:pid2669750"}, "units": ["legacy"]}


def _retired(generation: UUID, owner: UUID, pid: int, *, request: bool = False) -> dict[str, Any]:
    """`IncarnationResources` as the retired runtime wrote it: receipts carry pid
    and wall birth only."""
    requests: dict[str, Any] = {}
    if request:
        key = str(uuid4())
        requests[key] = {
            "request": key,
            "domain": str(uuid4()),
            "request_digest": "a" * 64,
            "deadline": "2026-09-26T23:00:00Z",
            "owner_process": {"pid": pid + 1, "birth": 1758900001.5},
            "root_process": {"pid": pid + 2, "birth": 1758900002.5},
        }
    return {
        "version": 1,
        "state": "admitted",
        "generation": str(generation),
        "owner": str(owner),
        "host_process": {"pid": pid, "birth": 1758900000.25},
        "frozen_by": None,
        "requests": requests,
    }


@dataclass
class Cluster:
    """The legacy-shaped database, the gateway home and the agents by role."""

    home: Path
    runner: str
    agents: dict[str, int]
    receipts: dict[str, int]

    def unit(self, machine: str) -> str:
        return str(self.home) if machine == GATEWAY else f"/homes/{machine}/.ava"


def _receipt(db: psycopg.Connection, aid: int, kind: str, before: dict[str, Any]) -> int:
    """The drain's applied restart (still claimed), or an applied and observed terminate."""
    terminate = kind == "terminate"
    row = db.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload,status,claimed_at,"
        "applied_at,observed_at,target_generation,target_owner) VALUES(%s,%s,"
        "'system:maintenance','',%s,%s,now(),now(),CASE WHEN %s THEN now() END,%s,%s) "
        "RETURNING id",
        (
            aid,
            kind,
            Jsonb(_DRAIN),
            "done" if terminate else "claimed",
            terminate,
            before["generation"],
            before["owner"],
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def _agent(
    db: psycopg.Connection,
    machine: str,
    *,
    resources: dict[str, Any] | None,
    receipt: str | None,
    live: bool = False,
    terminated: bool = False,
) -> tuple[int, int | None]:
    aid, _, _, _ = create_agent_row(spawner="user", machine=machine)
    command = _receipt(db, aid, receipt, resources) if receipt and resources else None
    runtime: tuple[Any, Any] = (None, None)
    if (live or terminated) and resources:
        runtime = (resources["generation"], resources["owner"])
    db.execute(
        "UPDATE agents_meta SET status=%s, runtime_kind=%s, runtime_generation=%s, "
        "runtime_owner=%s, lease_expires_at=%s, lifecycle_command_id=%s, "
        "incarnation_resources=%s WHERE id=%s",
        (
            "terminated" if terminated else ("running" if live else "idling"),
            "hosted" if live or terminated else None,
            *runtime,
            "2099-01-01T00:00:00Z" if live else None,
            command if receipt == "restart" else None,
            None if resources is None else Jsonb(resources),
            aid,
        ),
    )
    return aid, command


def _topology(db: psycopg.Connection, runner: str, cluster: Cluster) -> None:
    for name, paused in ((GATEWAY, False), (runner, False), (PAUSED, True), (GONE, False)):
        db.execute(
            "INSERT INTO machines(name, role, paused_at) VALUES(%s, '{agent-runner}', %s)",
            (name, "2026-08-14T00:00:00Z" if paused else None),
        )
        db.execute(
            "INSERT INTO machine_units(machine_name, home, serve_gateway, serve_agent_runner, "
            "url, up_since_at) VALUES(%s, %s, %s, true, 'http://x', now())",
            (name, cluster.unit(name), name == GATEWAY),
        )
    for name, posture in ((GATEWAY, "idle"), (runner, "paused"), (PAUSED, "paused")):
        db.execute(
            "INSERT INTO host_deploy_state(machine, posture, paused_at, updated_at) "
            "VALUES(%s, %s, CASE WHEN %s='paused' THEN now() END, now())",
            (name, posture, posture),
        )
    db.execute(
        "UPDATE deployment_state SET phase='updating', kind='update', holder='ubuntu:pid2669750', "
        "acquired_at='2026-09-24T10:00:00Z', expires_at='2026-09-24T10:10:00Z', "
        "managed_writer_evidence=%s WHERE id=1",
        (Jsonb({"version": 2, "current": None, "pending": _PENDING}),),
    )


@contextmanager
def _singletons(db: psycopg.Connection) -> Generator[None]:
    """`deployment_state` and `cluster_pin` survive the per-test truncation; restore them."""
    saved = db.execute(
        "SELECT (SELECT to_jsonb(d) FROM deployment_state d), (SELECT to_jsonb(p) FROM cluster_pin p)"
    ).fetchone()
    db.commit()
    assert saved is not None
    try:
        yield
    finally:
        db.rollback()
        for table, image in zip(("deployment_state", "cluster_pin"), saved, strict=True):
            db.execute(f"DELETE FROM {table}")  # noqa: S608 -- fixed table names
            db.execute(
                f"INSERT INTO {table} SELECT * FROM jsonb_populate_record(NULL::{table}, %s)",  # noqa: S608
                (Jsonb(image),),
            )
        db.commit()


@pytest.fixture
def cluster(db_conn: psycopg.Connection, tmp_path: Path) -> Iterator[Cluster]:
    home = tmp_path.resolve() / "gw-home"
    home.mkdir(mode=0o700)
    (home / "machine_name").write_text(GATEWAY)
    runner = machine_name()
    made = Cluster(home, runner, {}, {})
    with _singletons(db_conn):
        _topology(db_conn, runner, made)
        rows: dict[str, tuple[str, dict[str, Any] | None, str | None, dict[str, bool]]] = {
            "drained": (runner, _retired(uuid4(), uuid4(), 4242, request=True), "restart", {}),
            "terminated": (
                GATEWAY,
                _retired(uuid4(), uuid4(), 5151),
                "terminate",
                {"terminated": True},
            ),
            "live": (runner, _retired(uuid4(), uuid4(), 6161), None, {"live": True}),
            "paused": (PAUSED, _retired(uuid4(), uuid4(), 7171), "restart", {}),
            "unreceipted": (runner, _retired(uuid4(), uuid4(), 8181), None, {}),
            "late": (runner, _retired(uuid4(), uuid4(), 9191), "restart", {}),
            "null": (GATEWAY, None, None, {}),
        }
        for name, (machine, resources, receipt, flags) in rows.items():
            made.agents[name], command = _agent(
                db_conn, machine, resources=resources, receipt=receipt, **flags
            )
            if command is not None:
                made.receipts[name] = command
        made.agents["current"], _, _, _ = create_agent_row(spawner="user", machine=GATEWAY)
        birth = ResourceBirth(birth=uuid4()).model_dump(mode="json")
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(birth), made.agents["current"]),
        )
        db_conn.commit()
        yield made


def _connection(*, write: bool) -> psycopg.Connection:
    conn = psycopg.connect(settings.data_plane.db_url, autocommit=True)
    records.prepare_session(conn, write=write)
    return conn


def _state(db: psycopg.Connection) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT (SELECT jsonb_agg(to_jsonb(m) ORDER BY id) FROM agents_meta m), "
        "(SELECT jsonb_agg(to_jsonb(i) ORDER BY id) FROM inbound_messages i), "
        "(SELECT to_jsonb(d) FROM deployment_state d), "
        "(SELECT jsonb_agg(to_jsonb(u) ORDER BY machine_name) FROM machine_units u), "
        "(SELECT jsonb_agg(to_jsonb(h) ORDER BY machine) FROM host_deploy_state h)"
    ).fetchone()
    db.commit()
    assert row is not None
    return row


def _survey(inputs: Inputs) -> Any:
    with _connection(write=False) as conn, conn.transaction():
        return survey(conn, inputs)


def _attest(tmp: Path, cluster: Cluster, machine: str, *, census: bool = False) -> Path:
    """The real `cutover_inventory.py --attest` document for `machine`, over the
    rows `--check --rows-out` exports (recorded births predate this boot)."""
    rows = export_rows(_survey(Inputs()))
    facts = inventory.Facts(Path(cluster.unit(machine)), "slug", tmp, tmp, {}, {})
    if census:
        facts.processes.append({"pid": 1, "kind": "ava", "name": "agent-host"})
    path = tmp / f"attest-{machine}.json"
    path.write_text(json.dumps(inventory.attest(rows, machine, facts), indent=2, sort_keys=True))
    return path


def _inputs(tmp: Path, cluster: Cluster, **changes: Any) -> Inputs:
    attestations, raw = {}, {}
    for machine in (GATEWAY, cluster.runner):
        attestations[machine], raw[machine] = inventory.load_attestation(
            _attest(tmp, cluster, machine)
        )
    fields: dict[str, Any] = {
        "operator": "cutover-operator",
        "reason": "FC-4 cutover",
        "pending": _PENDING,
        "lease": _survey(Inputs()).lease,
        "retire_units": (RetiredUnit(machine=GONE, home=cluster.unit(GONE), evidence="host sold"),),
        "attestations": attestations,
        "raw": raw,
    }
    return Inputs(**(fields | changes))


def _verdicts(found: Any) -> dict[int, tuple[str, str | None]]:
    return {item.agent_id: (item.verdict, item.reason) for item in found.legacy}


def test_check_classifies_every_retired_row(cluster: Cluster) -> None:
    found = _survey(Inputs())
    agents = cluster.agents
    awaiting = ("awaiting", "no closure attestation for this machine")
    assert _verdicts(found) == {
        agents["drained"]: awaiting,
        agents["terminated"]: awaiting,
        agents["late"]: awaiting,
        agents["live"]: ("inadmissible", "the row still names a live old incarnation"),
        agents["paused"]: (
            "inadmissible",
            "the machine is paused; no closure evidence exists for it",
        ),
        agents["unreceipted"]: (
            "inadmissible",
            "no settled lifecycle receipt for the recorded incarnation",
        ),
    }
    assert found.counts == {
        "current_model": {GATEWAY: 1},
        "null_protocol_zero": {GATEWAY: 1},
        "unconverted": {"convertible": 0, "awaiting": 3, "inadmissible": 3},
    }


def test_check_reports_the_d_series(cluster: Cluster) -> None:
    checks = _survey(Inputs()).checks
    observed = {name: check["verdict"] for name, check in checks.items()} | {
        "pending": checks["D-1"]["pending"],
        "holder": checks["D-2"]["lease"]["holder"],
    }
    assert observed == {
        "D-1": "repair",
        "D-2": "repair",
        "D-3": "info",
        "D-4": "ok",
        "D-5": "info",
        "D-6": "repair",
        "D-7": "info",
        "D-8": "repair",
        "D-9": "info",
        "D-10": "attention",  # the throwaway database is owned by its bootstrap superuser
        "D-11": "ok",
        "D-12": "info",
        "D-13": "info",
        "D-14": "info",
        "pending": _PENDING,
        "holder": "ubuntu:pid2669750",
    }


def test_check_exports_every_identity_and_writes_nothing(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    unchanged = _state(db_conn)
    exported = export_rows(_survey(Inputs()))
    # The drained row's host, exec owner and exec root receipts, for --attest.
    drained = sorted(
        (r["role"], r["pid"]) for r in exported if r["agent_id"] == cluster.agents["drained"]
    )
    assert drained == [("host", 4242), ("owner", 4243), ("root", 4244)]
    rows_out = tmp_path / "rows.json"
    with _patched_session(write=False):
        code = records.main(["--home", str(cluster.home), "--check", "--rows-out", str(rows_out)])
    written = json.loads(rows_out.read_text())
    assert (code, written) == (2, json.loads(json.dumps(exported)))
    assert _state(db_conn) == unchanged
    assert not (cluster.home / records.RECORD).exists()


@contextmanager
def _patched_session(*, write: bool) -> Generator[None]:
    @contextmanager
    def session(_home: Path, _registry: Path, *, write: bool) -> Generator[psycopg.Connection]:
        with _connection(write=write) as conn:
            yield conn

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(records, "session", session)
        yield


def test_dry_run_plans_every_repair_with_its_before_image_and_writes_nothing(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    unchanged = _state(db_conn)
    inputs = _inputs(tmp_path, cluster)
    plan = records.plan_repairs(_survey(inputs), inputs, (GATEWAY, str(cluster.home)))
    assert plan.refusals == []
    effects = plan.effects
    assert effects["pending"] == [
        {
            "op": "pending-clear",
            "before": {"version": 2, "current": None, "pending": _PENDING},
            "after": None,
        }
    ]
    assert effects["lease"][0]["before"]["holder"] == "ubuntu:pid2669750"
    assert [(e["machine"], e["before"]["posture"]) for e in effects["posture"]] == [
        (cluster.runner, "paused")
    ]
    assert [(e["machine"], e["evidence"]) for e in effects["units"]] == [(GONE, "host sold")]
    closes = {e["agent_id"]: e for e in effects["incarnations"]}
    assert set(closes) == {cluster.agents[name] for name in ("drained", "terminated", "late")}
    drained = closes[cluster.agents["drained"]]
    assert drained["receipt"] == cluster.receipts["drained"]
    assert drained["attestation_sha256"] == inputs.digest(cluster.runner)
    assert _state(db_conn) == unchanged


def _run(cluster: Cluster, inputs: Inputs) -> dict[str, Any] | None:
    with _connection(write=True) as conn:
        return records.execute(conn, cluster.home, inputs, (GATEWAY, str(cluster.home)))


def test_execute_repairs_every_record(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    run = _run(cluster, _inputs(tmp_path, cluster))
    assert run is not None
    repaired = _survey(Inputs())
    observed = {
        "results": run["results"],
        "evidence": repaired.evidence,
        "lease": repaired.lease,
        "postures": dict(db_conn.execute("SELECT machine, posture FROM host_deploy_state")),
        "units": sorted(
            row[0] for row in db_conn.execute("SELECT machine_name FROM machine_units")
        ),
        "unconverted": repaired.counts["unconverted"],
    }
    db_conn.commit()
    assert observed == {
        "results": {
            "pending": ["applied"],
            "lease": ["applied"],
            "posture": ["applied"],
            "units": ["applied"],
            "incarnations": ["applied", "applied", "applied"],
        },
        "evidence": None,
        "lease": FREE_LEASE,
        "postures": {GATEWAY: "idle", cluster.runner: "idle", PAUSED: "paused"},
        "units": sorted([GATEWAY, cluster.runner, PAUSED]),
        "unconverted": {"convertible": 0, "awaiting": 0, "inadmissible": 3},
    }


def test_execute_records_the_run_and_the_same_inputs_change_nothing(
    cluster: Cluster, tmp_path: Path
) -> None:
    inputs = _inputs(tmp_path, cluster)
    run = _run(cluster, inputs)
    record = cluster.home / records.RECORD
    journal = json.loads((record / "journal.json").read_text())
    befores = [
        e["before"]["host_process"]["pid"] for e in journal["runs"][0]["effects"]["incarnations"]
    ]
    assert sorted(befores) == [4242, 5151, 9191]
    stored = {
        machine: (record / "attestations" / f"{hashlib.sha256(raw).hexdigest()}.json").read_bytes()
        for machine, raw in inputs.raw.items()
    }
    assert stored == inputs.raw
    assert (record / "journal.json").stat().st_mode & 0o777 == 0o600

    assert _run(cluster, inputs) == run
    fresh = _inputs(tmp_path, cluster, pending=None, lease=None, retire_units=())
    assert _run(cluster, fresh) is None
    assert len(json.loads((record / "journal.json").read_text())["runs"]) == 1
    after = _survey(inputs).checks
    verdicts = {name: after[name]["verdict"] for name in ("D-1", "D-2", "D-6", "D-8")}
    assert verdicts == dict.fromkeys(("D-1", "D-2", "D-6", "D-8"), "ok")


async def test_the_converted_agent_carries_its_evidence_and_is_admitted(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path, aops_pool: AsyncConnectionPool
) -> None:
    inputs = _inputs(tmp_path, cluster)
    _run(cluster, inputs)
    drained = cluster.agents["drained"]
    row = db_conn.execute(
        "SELECT m.incarnation_resources, i.payload->'cutover_closure' FROM agents_meta m "
        "JOIN inbound_messages i ON i.id=m.lifecycle_command_id WHERE m.id=%s",
        (drained,),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    closed, closure = decode_resources(row[0]), row[1]
    assert isinstance(closed, IncarnationResources)
    observed = (
        closed.host_process,
        closed.requests,
        closure["attestation_sha256"],
        closure["operator"],
    )
    assert observed == (None, {}, inputs.digest(cluster.runner), "cutover-operator")
    successor = await admit_hosted_runtime(
        aops_pool, drained, cluster.runner, uuid4(), expected_from="idling"
    )
    assert successor is not None


def test_a_crash_after_a_commit_resumes_with_the_same_inputs_only(
    cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path, cluster)
    real = records._write_journal
    writes: list[int] = []

    def crash_after_first_conversion(home: Path, journal: dict[str, Any]) -> None:
        run = journal["runs"][-1]
        if run["results"]["incarnations"] and not writes:
            writes.append(1)
            raise OSError("simulated crash between the commit and the journal write")
        real(home, journal)

    monkeypatch.setattr(records, "_write_journal", crash_after_first_conversion)
    with pytest.raises(OSError, match="simulated crash"):
        _run(cluster, inputs)
    monkeypatch.setattr(records, "_write_journal", real)

    with pytest.raises(records.RefusedError, match="other inputs"):
        _run(cluster, replace(inputs, reason="another reason"))
    run = _run(cluster, inputs)
    assert run is not None
    assert run["results"]["incarnations"] == ["already", "applied", "applied"]
    journal = json.loads((cluster.home / records.JOURNAL).read_text())
    assert len(journal["runs"]) == 1


def _refusals(cluster: Cluster, inputs: Inputs) -> list[str]:
    return records.plan_repairs(_survey(inputs), inputs, (GATEWAY, str(cluster.home))).refusals


def test_repairs_refuse_without_exact_operator_evidence(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    unchanged = _state(db_conn)
    assert _refusals(cluster, _inputs(tmp_path, cluster, pending=None, lease=None)) == [
        "a pending publication is recorded; supply its exact JSON (D-1) as --pending-json",
        "a legacy deploy lease is held (ubuntu:pid2669750); supply its exact columns (D-2) as "
        "--lease-json",
    ]
    stale = _inputs(
        tmp_path,
        cluster,
        pending=_PENDING | {"units": []},
        lease=FREE_LEASE,
        retire_units=(
            RetiredUnit(machine=PAUSED, home=cluster.unit(PAUSED), evidence="x"),
            RetiredUnit(machine=GATEWAY, home=str(cluster.home), evidence="x"),
        ),
    )
    assert _refusals(cluster, stale) == [
        "--pending-json differs from the recorded pending publication",
        "--lease-json differs from the recorded deploy lease",
        f"--retire-units: {PAUSED}:{cluster.unit(PAUSED)} belongs to a paused machine; its "
        "units are kept",
        f"--retire-units: {GATEWAY}:{cluster.home} is this gateway's own unit",
        "the pending, lease and posture repairs need a closure attestation from every included "
        f"machine; missing: {GONE}",
    ]
    with pytest.raises(records.RefusedError, match="--pending-json differs"):
        _run(cluster, stale)
    assert _state(db_conn) == unchanged
    assert not (cluster.home / records.JOURNAL).exists()


def test_cluster_wide_repairs_need_every_included_machine_to_prove_it_stopped(
    cluster: Cluster, tmp_path: Path
) -> None:
    base = _inputs(tmp_path, cluster)
    without_gateway = replace(
        base,
        attestations={cluster.runner: base.attestations[cluster.runner]},
        raw={cluster.runner: base.raw[cluster.runner]},
    )
    assert _refusals(cluster, without_gateway) == [
        "the pending, lease and posture repairs need a closure attestation from every included "
        f"machine; missing: {GATEWAY}"
    ]
    live, raw = inventory.load_attestation(_attest(tmp_path, cluster, GATEWAY, census=True))
    busy = replace(
        base, attestations=base.attestations | {GATEWAY: live}, raw=base.raw | {GATEWAY: raw}
    )
    assert _refusals(cluster, busy) == [
        f"the attestation for {GATEWAY} does not prove closure (all_absent True, "
        "census_empty False)"
    ]


def test_an_unattested_identity_stays_inadmissible_and_a_later_run_converts_it(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    inputs = _inputs(tmp_path, cluster)
    late = cluster.agents["late"]
    moved = _retired(uuid4(), uuid4(), 9999)
    before = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (late,)
    ).fetchone()
    assert before is not None
    moved |= {"generation": before[0]["generation"], "owner": before[0]["owner"]}
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s", (Jsonb(moved), late)
    )
    db_conn.commit()
    assert _verdicts(_survey(inputs))[late] == (
        "inadmissible",
        "the attestation does not prove 1 recorded identity(ies) gone",
    )
    run = _run(cluster, inputs)
    assert run is not None and len(run["results"]["incarnations"]) == 2

    fresh = _inputs(tmp_path, cluster, pending=None, lease=None, retire_units=())
    second = _run(cluster, fresh)
    assert second is not None and second["results"]["incarnations"] == ["applied"]
    journal = json.loads((cluster.home / records.JOURNAL).read_text())
    assert [run["state"] for run in journal["runs"]] == ["done", "done"]


def test_cleared_publication_keeps_current_and_refuses_what_admission_cannot_read(
    cluster: Cluster, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    from scripts.cutover_db_survey import cleared_publication

    current = {"publication_id": "not-a-uuid"}
    assert cleared_publication({"version": 2, "current": current, "pending": _PENDING}) == {
        "version": 2,
        "current": current,
        "pending": None,
    }
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb({"version": 2, "current": current, "pending": _PENDING}),),
    )
    db_conn.commit()
    refusals = _refusals(cluster, _inputs(tmp_path, cluster))
    assert refusals[0].startswith("clearing pending leaves evidence the runtime cannot read")


def test_owner_authority_names_the_home_socket_port_owner_and_database(tmp_path: Path) -> None:
    from shared.pg_admin import pg_socket_path

    home = tmp_path.resolve() / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_DB_URL=postgresql://ava_main@127.0.0.1:6433/ava_main\n")
    registry = tmp_path / "clusters.json"
    record = {
        "ports": {"postgres": 25433, "gateway": 25400},
        "gateway_home": str(home),
        "created_at": "2026-08-01T00:00:00+00:00",
        "data_plane_host": "",
    }
    registry.write_text(json.dumps({str(home): record}))
    authority = records.owner_authority(home, registry)
    assert (authority.owner, authority.database, authority.data_dir) == (
        "ava_main",
        "ava_main",
        home / "pg",
    )
    assert f"host={pg_socket_path(home)}&port=25433" in authority.admin_url
    with pytest.raises(records.RefusedError, match="no registry record"):
        records.owner_authority(home, tmp_path / "missing.json")


def test_execute_requires_the_operator_and_reason(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    assert records.main(["--home", str(home), "--execute"]) == 1


def test_legacy_commit_compares_the_pin_and_the_applied_migrations(
    cluster: Cluster, db_conn: psycopg.Connection
) -> None:
    head = records.resolve_commit("HEAD")
    found = _survey(Inputs(legacy_commit=head))
    assert found.checks["D-3"]["verdict"] == "attention"  # the throwaway pin names no commit
    assert found.checks["D-4"]["equals"] == ["legacy", "current"]
    db_conn.execute("UPDATE cluster_pin SET target_sha=%s WHERE id=1", (head,))
    db_conn.commit()
    assert _survey(Inputs(legacy_commit=head)).checks["D-3"]["verdict"] == "ok"
    with pytest.raises(records.RefusedError, match="not a commit"):
        records.resolve_commit("refs/heads/no-such-branch-for-fc4")
