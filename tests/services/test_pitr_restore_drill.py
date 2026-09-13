"""The operator restore drill: target parsing, acceptance gates and sequence."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from services.pitr import restore_drill
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.restore_drill import (
    DRILL_TABLES,
    DrillError,
    DrillEvidence,
    DrillRequest,
    parse_target_wall,
    run_restore_drill,
)
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_postgres import SandboxPostgresIdentity
from services.pitr.restore_proof import LivePostgresIdentity

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


class _RecordingReader:
    def __init__(self) -> None:
        self.downloads: list[Path] = []

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        self.downloads.append(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ciphertext")


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _fake_process() -> subprocess.Popen[str]:
    return cast("subprocess.Popen[str]", _FakeProcess())


def _discard(_message: str) -> None:
    pass


def _noop(*args: object, **kwargs: object) -> None:
    pass


def _candidate() -> CandidateManifest:
    return CandidateManifest(
        1,
        "activation-test-chain",
        False,
        17,
        "ava_main",
        "7683562760506812143",
        16 * 1024 * 1024,
        1,
        "0/1000000",
        "0/2000000",
        (WalRange(1, "0/1000000", "0/2000000"),),
        BaseObject("base", "7", 1, "crc", "crc32c", "crc", "sha", 1, "key", "AVAPITRB1"),
        "native",
        "backup_manifest",
        "base",
        "7",
        "migrations",
    )


def _request(tmp_path: Path) -> DrillRequest:
    return DrillRequest(
        candidate=_candidate(),
        reader=_RecordingReader(),
        key=b"key",
        ack_dir=tmp_path / "ack",
        scratch=tmp_path / "scratch",
        target_lsn="0/1800000",
        target_wall=parse_target_wall("2026-09-13 13:13:03+08"),
        pg_ctl=tmp_path / "pg_ctl",
        pg_verifybackup=tmp_path / "pg_verifybackup",
        live_db_url="postgresql://live",
        data_directory=str(tmp_path / "pg"),
    )


def _evidence(request: DrillRequest) -> DrillEvidence:
    return DrillEvidence(
        chain_id=request.candidate.chain_id,
        target_lsn=request.target_lsn,
        target_wall=request.target_wall.isoformat(),
        scratch=str(request.scratch),
        started_at="2026-09-13T09:00:00+00:00",
    )


def _live_identity() -> LivePostgresIdentity:
    return LivePostgresIdentity(
        4242, 1789272331.91, "/var/lib/pg", "7683562760506812143", "2026-09-13 04:05:32+00", "p"
    )


def _sandbox_identity(pgdata: Path) -> SandboxPostgresIdentity:
    return SandboxPostgresIdentity(4242, 1.0, os.getpgrp(), str(pgdata.resolve()))


def _passing_criteria() -> dict[str, Any]:
    return {
        "identity": {
            "system_identifier": "7683562760506812143",
            "expected_system_identifier": "7683562760506812143",
            "server_version_num": 170004,
            "server_version_major": 17,
            "expected_major": 17,
            "timeline": "2",
            "expected_timeline": 1,
            "database": "ava_main",
        },
        "double_face": {
            "recent_count": 5,
            "max_created_at": "2026-09-13 13:13:01+08",
            "within_tolerance": True,
            "tolerance_seconds": 300,
        },
        "counts_restored": dict.fromkeys(DRILL_TABLES, 1),
        "counts_live": dict.fromkeys(DRILL_TABLES, 2),
        "business_rows": [["1", "done", "title"]],
        "live_after": {},
        "live_unchanged": True,
        "teardown": {"stopped": True, "error": None},
        "residue": {"processes": [], "port_listening": False, "pid_file_present": False},
    }


def test_target_wall_requires_an_explicit_utc_offset() -> None:
    parsed = parse_target_wall("2026-09-13 13:13:03+08")
    assert parsed.utcoffset() == timedelta(hours=8)
    assert parse_target_wall("2026-09-13T13:13:03Z").utcoffset() == timedelta(0)
    with pytest.raises(DrillError, match="UTC offset"):
        parse_target_wall("2026-09-13 13:13:03")
    with pytest.raises(DrillError, match="ISO-8601"):
        parse_target_wall("2026-09-13 13:13:03 CST")


def test_drill_tables_are_real_schema_tables() -> None:
    """Driver bug B2: the drill counted `tasks`, which does not exist.

    The three application tables are pinned against the baseline; `checkpoints`
    belongs to the upstream LangGraph checkpoint schema (its name is a wire
    constraint, so it is pinned here as a literal).
    """
    schema = _SCHEMA_SQL.read_text()
    for table in ("inbound_messages", "agent_tasks", "agents"):
        assert f"CREATE TABLE {table} (" in schema, table
    assert "checkpoints" in DRILL_TABLES
    assert "agent_tasks" in DRILL_TABLES
    assert "tasks" not in DRILL_TABLES


def test_target_lsn_bounds() -> None:
    candidate = _candidate()
    with pytest.raises(DrillError, match="precedes the chain start"):
        restore_drill._require_target_lsn(candidate, "0/0")
    with pytest.raises(DrillError, match="invalid PostgreSQL LSN"):
        restore_drill._require_target_lsn(candidate, "nonsense")
    restore_drill._require_target_lsn(candidate, "0/1000000")
    # A target beyond the candidate's recorded end LSN stays valid: the chain
    # keeps archiving, and the ACK evidence is the upper bound (the 2026-09-13
    # drill ran at 26/A03520B0 with a recorded end LSN of 26/51007328).
    restore_drill._require_target_lsn(candidate, "26/A03520B0")


def test_fresh_scratch_is_required(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "leftover").write_text("x")
    with pytest.raises(DrillError, match="not fresh"):
        restore_drill._require_fresh_scratch(occupied)
    empty = tmp_path / "empty"
    empty.mkdir()
    restore_drill._require_fresh_scratch(empty)
    fresh = tmp_path / "fresh"
    restore_drill._require_fresh_scratch(fresh)
    assert fresh.is_dir()


def test_prepare_pgdata_uses_the_extraction_return_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driver bug B1: verifybackup ran against `<scratch>/sandbox` (the parent)
    instead of the extracted pgdata the extractor returned."""
    request = _request(tmp_path)
    restore_drill._require_fresh_scratch(request.scratch)
    evidence = _evidence(request)
    extracted = request.scratch / "sandbox" / "data"
    seen: dict[str, Any] = {}

    def fake_restore_object(candidate: CandidateManifest) -> RestoreObject:
        return RestoreObject("base.enc", "prefix/base.enc", "pin", 10, "crc32c", "AAAAAA==", ())

    def fake_authenticate(source: Path, *, key: bytes, expected: RestoreObject) -> None:
        pass

    def fake_extract(source: Path, destination: Path, **kwargs: object) -> Path:
        destination.mkdir(parents=True)
        extracted.mkdir(parents=True)
        return extracted

    def fake_wal_objects(
        *, ack_dir: Path, archive_names: tuple[str, ...]
    ) -> tuple[RestoreObject, ...]:
        return ()

    def fake_download_wal(**kwargs: object) -> None:
        pass

    def fake_verify(command: list[str], *, timeout: float) -> None:
        seen["verify"] = [str(part) for part in command]

    monkeypatch.setattr(restore_drill, "_base_restore_object", fake_restore_object)
    monkeypatch.setattr(restore_drill, "authenticate_base_ciphertext", fake_authenticate)
    monkeypatch.setattr(restore_drill, "extract_authenticated_base", fake_extract)
    monkeypatch.setattr(restore_drill, "wal_objects_from_acks", fake_wal_objects)
    monkeypatch.setattr(restore_drill, "_download_wal", fake_download_wal)
    monkeypatch.setattr(restore_drill, "_run", fake_verify)

    pgdata = restore_drill._prepare_pgdata(request, evidence, _discard)
    assert pgdata == extracted
    assert seen["verify"][-1] == str(extracted)
    assert seen["verify"][-1] != str(request.scratch / "sandbox")


def test_run_sandbox_tears_down_when_criteria_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    request.scratch.mkdir(parents=True)
    evidence = _evidence(request)
    pgdata = request.scratch / "sandbox" / "data"
    pgdata.mkdir(parents=True)
    stopped: list[SandboxPostgresIdentity] = []

    def blow_up(*args: object, **kwargs: object) -> None:
        raise DrillError("criteria blew up")

    def fake_free_port() -> int:
        return 55432

    def fake_write_config(*args: object, **kwargs: object) -> Path:
        return tmp_path / "pg.conf"

    def fake_spawn(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        return _fake_process()

    def fake_wait_identity(*args: object, **kwargs: object) -> SandboxPostgresIdentity:
        return _sandbox_identity(pgdata)

    def fake_stop(pg_ctl: Path, pgdata_arg: Path, sandbox: SandboxPostgresIdentity) -> None:
        stopped.append(sandbox)

    def fake_residue(scratch: Path, port: int, pgdata_arg: Path) -> dict[str, object]:
        return {"processes": [], "port_listening": False, "pid_file_present": False}

    monkeypatch.setattr(restore_drill, "_free_port", fake_free_port)
    monkeypatch.setattr(restore_drill, "_append_recovery_config", _noop)
    monkeypatch.setattr(restore_drill, "_write_sandbox_config", fake_write_config)
    monkeypatch.setattr(restore_drill, "_spawn_sandbox_postgres", fake_spawn)
    monkeypatch.setattr(restore_drill, "_wait_for_sandbox_identity", fake_wait_identity)
    monkeypatch.setattr(restore_drill, "_wait_for_promotion", _noop)
    monkeypatch.setattr(restore_drill, "_collect_criteria", blow_up)
    monkeypatch.setattr(restore_drill, "_stop_sandbox", fake_stop)
    monkeypatch.setattr(restore_drill, "_residue_scan", fake_residue)

    with pytest.raises(DrillError, match="criteria blew up"):
        restore_drill._run_sandbox(request, evidence, pgdata, _live_identity(), _discard)
    assert len(stopped) == 1
    assert stopped[0].data_directory == str(pgdata.resolve())
    assert evidence.criteria["teardown"]["stopped"] is True
    assert evidence.criteria["residue"]["processes"] == []


def test_run_restore_drill_writes_failure_evidence_and_keeps_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)

    def explode(*args: object, **kwargs: object) -> Path:
        raise DrillError("download exploded")

    monkeypatch.setattr(restore_drill, "_require_group_leader", _no_leader_check)
    monkeypatch.setattr(restore_drill, "_live_identity", _stub_live_identity)
    monkeypatch.setattr(restore_drill, "_prepare_pgdata", explode)

    with pytest.raises(DrillError, match="download exploded"):
        run_restore_drill(request)
    evidence_path = request.scratch / "drill-evidence.json"
    payload = json.loads(evidence_path.read_text())
    assert payload["outcome"] == "fail"
    assert "download exploded" in payload["error"]
    assert evidence_path.stat().st_mode & 0o777 == 0o600


def test_run_restore_drill_passes_when_every_check_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    pgdata = request.scratch / "sandbox" / "data"

    def fake_prepare(request_arg: DrillRequest, evidence: DrillEvidence, say: object) -> Path:
        pgdata.mkdir(parents=True)
        return pgdata

    def fake_sandbox(
        request_arg: DrillRequest,
        evidence: DrillEvidence,
        pgdata_arg: Path,
        live_before: LivePostgresIdentity,
        say: object,
    ) -> None:
        evidence.criteria.update(_passing_criteria())

    monkeypatch.setattr(restore_drill, "_require_group_leader", _no_leader_check)
    monkeypatch.setattr(restore_drill, "_live_identity", _stub_live_identity)
    monkeypatch.setattr(restore_drill, "_prepare_pgdata", fake_prepare)
    monkeypatch.setattr(restore_drill, "_run_sandbox", fake_sandbox)

    evidence = run_restore_drill(request)
    assert evidence.outcome == "pass"
    payload = json.loads((request.scratch / "drill-evidence.json").read_text())
    assert payload["outcome"] == "pass"
    assert payload["chain_id"] == "activation-test-chain"


class _FakeScanProcess:
    def __init__(self, pid: int, cmdline: list[str]) -> None:
        self.info: dict[str, object] = {"pid": pid, "cmdline": cmdline}


def test_residue_scan_ignores_its_own_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drill's own `--scratch` argv must not trip the residue gate.

    QA on PR #2358 reproduced the opposite: a bare containment match flagged
    the invoking process chain, so every runbook-shaped invocation would have
    false-failed its isolation criterion. The scan now skips this process and
    its ancestors, requires a path inside the tree (never the bare root), and
    only counts `postgres` executables.
    """
    scratch = tmp_path / "scratch"
    pgdata = scratch / "sandbox" / "data"
    rows = [
        _FakeScanProcess(os.getpid(), ["ava", "pitr", "drill", "--scratch", str(scratch)]),
        _FakeScanProcess(990001, ["bash", "-c", f"ava pitr drill --scratch {scratch}"]),
        _FakeScanProcess(990002, ["tail", "-f", f"{scratch}/sandbox-postgres.log"]),
        _FakeScanProcess(990003, ["/usr/bin/postgres", "-D", "/somewhere/else/data"]),
        _FakeScanProcess(
            990004, ["/usr/lib/postgresql/17/bin/postgres", "-D", f"{scratch}/sandbox/data"]
        ),
    ]

    def fake_process_iter(*args: object, **kwargs: object) -> Iterator[_FakeScanProcess]:
        return iter(rows)

    monkeypatch.setattr(restore_drill.psutil, "process_iter", fake_process_iter)
    result = restore_drill._residue_scan(scratch, 1, pgdata)

    assert result["processes"] == [f"/usr/lib/postgresql/17/bin/postgres -D {scratch}/sandbox/data"]
    assert result["port_listening"] is False
    assert result["pid_file_present"] is False


def test_acceptance_failures_cover_the_criteria_set(tmp_path: Path) -> None:
    request = _request(tmp_path)
    failures = restore_drill._acceptance_failures(_evidence(request))
    assert any("identity" in failure for failure in failures)
    assert any("double-face" in failure for failure in failures)
    assert any("live PostgreSQL" in failure for failure in failures)
    assert any("teardown" in failure for failure in failures)


def test_acceptance_failures_flag_a_surviving_sandbox(tmp_path: Path) -> None:
    request = _request(tmp_path)
    evidence = _evidence(request)
    evidence.criteria.update(_passing_criteria())
    evidence.criteria["teardown"] = {"stopped": False, "error": "cannot be reaped"}
    evidence.criteria["residue"] = {
        "processes": ["postgres -D /scratch/sandbox/data"],
        "port_listening": True,
        "pid_file_present": True,
    }
    failures = restore_drill._acceptance_failures(evidence)
    assert any("teardown did not complete" in failure for failure in failures)
    assert any("port still accepts" in failure for failure in failures)
    assert any("pid file remains" in failure for failure in failures)


def _no_leader_check() -> None:
    pass


def _stub_live_identity(*args: object) -> LivePostgresIdentity:
    return _live_identity()
