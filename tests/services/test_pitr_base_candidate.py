"""Fail-closed preflights and diagnostics around the physical base candidate.

The 2026-08-30 activation died at `base_pending`: pg_basebackup exited 1 with
"no pg_hba.conf entry for replication connection" while every normal-connection
probe passed, and the only durable record was the bare type name. These tests
lock the two fixes: the pg_hba replication-rule preflight and the stderr-carrying
failure messages.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from services.pitr import base_candidate
from services.pitr.base_candidate import (
    BaseCandidateError,
    _rule_name_set,
    _stderr_suffix,
    _validate_replication_contract,
    _validate_replication_hba,
)

# ─── completed-candidate reconciliation (QA #1147 C2) ────────────────────────


def _reconcile_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checksum_algo: str,
    checksum_value: str,
    crc32c: str,
) -> tuple[Path, Path]:
    import json
    from dataclasses import asdict

    from services.pitr.base_candidate import CandidateFacts
    from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
    from services.pitr.base_stream import BaseEncryptionPlan

    chain_id = "chain-1"
    root = tmp_path / "root"
    ready = root / "base-candidates" / f"{chain_id}.ready"
    ready.mkdir(parents=True)
    (ready / "backup_manifest").write_text("{}")
    (root / "base-facts").mkdir(parents=True)
    facts = CandidateFacts(17, "sysid", 16 * 1024 * 1024, 1, "mig", "ava")
    ranges = (WalRange(1, "0/1000000", "0/2000000"),)

    def load_facts(_root: Path, _chain_id: str) -> CandidateFacts:
        return facts

    def parse_manifest(_path: Path) -> tuple[str, str, tuple[WalRange, ...]]:
        return ("sysid", "0/1000000", ranges)

    def snapshot(_ready: Path) -> tuple[list[object], str]:
        return ([], "sha")

    monkeypatch.setattr(base_candidate, "_load_facts", load_facts)
    monkeypatch.setattr(base_candidate, "parse_native_manifest", parse_manifest)
    monkeypatch.setattr(base_candidate, "snapshot_candidate", snapshot)

    candidate = CandidateManifest(
        schema_version=1,
        chain_id=chain_id,
        protected=False,
        postgres_major=17,
        database_name="ava",
        system_identifier="sysid",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/1000000",
        end_lsn="0/2000000",
        wal_ranges=ranges,
        base_object=BaseObject(
            "base", "1", 100, crc32c, checksum_algo, checksum_value, "sha", 90, "key", "AVAPITRB1"
        ),
        native_manifest_sha256="native",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_pin_token="1",  # noqa: S106 — test fixture
        migration_set_sha256="mig",
    )
    (root / "base-manifests").mkdir(parents=True)
    (root / "base-manifests" / f"{chain_id}.candidate.json").write_text(candidate.to_json())

    plan = BaseEncryptionPlan(1, "sha", 100, "native", "nonce", "base", "key", 1, 100, "crc-plan")
    plan_path = root / "base-plans" / f"{chain_id}.plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(asdict(plan)))

    class Source:
        ciphertext_size = 100

    def load_source(*_a: object, **_k: object) -> tuple[Source, BaseEncryptionPlan]:
        return (Source(), plan)

    monkeypatch.setattr(base_candidate, "load_or_create_source", load_source)

    return root, ready


def test_reconcile_accepts_non_crc32c_ack_with_matching_local_crc32c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA #1147 C2: a Baidu-flavored candidate (md5 ACK) reconciles when the
    local plan's crc32c matches — the algo-aware comparison never rejects a
    non-crc32c vocabulary outright."""
    root, ready = _reconcile_case(
        tmp_path, monkeypatch, checksum_algo="md5", checksum_value="md5v", crc32c="crc-plan"
    )
    base_candidate.reconcile_completed_candidates(root, key=b"k" * 32, key_id="key")
    assert not ready.exists()


def test_reconcile_rejects_mismatched_local_crc32c_for_non_crc32c_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ready = _reconcile_case(
        tmp_path, monkeypatch, checksum_algo="md5", checksum_value="md5v", crc32c="other"
    )
    with pytest.raises(BaseCandidateError, match="evidence does not match"):
        base_candidate.reconcile_completed_candidates(root, key=b"k" * 32, key_id="key")
    assert ready.exists()


def test_reconcile_rejects_crc32c_ack_differing_from_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ready = _reconcile_case(
        tmp_path, monkeypatch, checksum_algo="crc32c", checksum_value="other", crc32c="crc-plan"
    )
    with pytest.raises(BaseCandidateError, match="evidence does not match"):
        base_candidate.reconcile_completed_candidates(root, key=b"k" * 32, key_id="key")
    assert ready.exists()


def test_stderr_suffix_bounds_decodes_and_preserves_the_tail() -> None:
    assert _stderr_suffix(None) == ""
    assert _stderr_suffix(b"") == ""
    assert _stderr_suffix(b"boom\n") == ": boom"
    assert _stderr_suffix("ü".encode()) == ": ü"
    assert _stderr_suffix("ü".encode("latin-1")) == ": \ufffd"
    payload = "x" * 5000
    assert _stderr_suffix(payload.encode()) == f": {payload[-1600:]}"


def test_run_capture_failure_carries_the_child_stderr(tmp_path: Path) -> None:
    """A failing capture must name the exit code AND the child's stderr tail —
    the 2026-08-30 record held neither."""
    command = [
        sys.executable,
        "-c",
        "import sys; print('boom-detail', file=sys.stderr); sys.exit(3)",
    ]
    with pytest.raises(BaseCandidateError) as caught:
        base_candidate._run_capture(
            command,
            env={"PGPASSWORD": "x"},
            owner=tmp_path / "owner.json",
            stop=threading.Event(),
        )
    message = str(caught.value)
    assert "exited 3" in message
    assert "boom-detail" in message


# ─── pg_hba replication-rule preflight ───────────────────────────────────────


def test_rule_name_set_parses_plain_and_quoted_names() -> None:
    assert _rule_name_set("{all}") == frozenset({"all"})
    assert _rule_name_set("{all,replication}") == frozenset({"all", "replication"})
    assert _rule_name_set('{"ava_pitr_repl"}') == frozenset({"ava_pitr_repl"})
    assert _rule_name_set(None) == frozenset()


class _FakeCursor:
    def __init__(self, rows: list[tuple[str, str, str | None]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str) -> None:
        return None

    def fetchall(self) -> list[tuple[str, str, str | None]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[str, str, str | None]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


@pytest.mark.parametrize(
    ("rules", "covers"),
    [
        ([("{all}", "{all}", None)], False),  # `all` never matches replication
        ([("{replication}", "{all}", None)], True),
        ([("{replication}", "{someone_else}", None)], False),
        ([("{replication}", "{ava_pitr_repl,other}", None)], True),
        ([("{all,replication}", '{"ava_pitr_repl"}', None)], True),
        ([], False),
        # A replication row with a parse error is NOT in effect: the view still
        # lists it, so it must not count as coverage (QA #1096 P2).
        ([("{replication}", "{ava_pitr_repl}", "authentication method not supported")], False),
    ],
)
def test_validate_replication_hba_requires_a_rule_covering_the_role(
    monkeypatch: pytest.MonkeyPatch,
    rules: list[tuple[str, str, str | None]],
    covers: bool,
) -> None:
    monkeypatch.setattr("services.pitr.activation_runtime.pitr_admin_url", lambda: "admin-url")

    def _connect(_url: str) -> _FakeConn:
        return _FakeConn(rules)

    monkeypatch.setattr(base_candidate.psycopg, "connect", _connect)
    replication = {"user": "ava_pitr_repl", "host": "127.0.0.1", "port": "5433"}
    if covers:
        _validate_replication_hba(replication)
    else:
        with pytest.raises(BaseCandidateError, match="physical-replication rule"):
            _validate_replication_hba(replication)


def test_validate_replication_hba_wraps_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_url: str) -> _FakeConn:
        raise RuntimeError("registry missing")

    monkeypatch.setattr("services.pitr.activation_runtime.pitr_admin_url", lambda: "admin-url")
    monkeypatch.setattr(base_candidate.psycopg, "connect", boom)
    with pytest.raises(BaseCandidateError, match="cannot verify the replication pg_hba"):
        _validate_replication_hba({"user": "ava_pitr_repl", "host": "127.0.0.1"})


# ─── the full contract check against real PostgreSQL ────────────────────────


def _scratch_pg(tmp_path: Path) -> tuple[Path, Path, int]:
    from shared.pg_tools import pg_tool

    port = 39618
    sock = Path(tempfile.mkdtemp(prefix="ava-pg-sock-", dir="/tmp"))
    data = tmp_path / "pg"
    log = tmp_path / "pg.log"
    subprocess.run(  # noqa: S603
        [pg_tool("initdb"), "-D", str(data), "-U", "ava", "-A", "trust"],
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [
            pg_tool("pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(log),
            "-w",
            "-t",
            "30",
            "start",
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories={sock} "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    return data, sock, port


def test_validate_replication_contract_fails_closed_without_replication_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG 17: a `host all all` loopback row passes every normal-connection
    probe yet still refuses pg_basebackup — the preflight must catch exactly
    that state BEFORE the backup runs."""
    from shared.pg_tools import pg_tool

    data, sock, port = _scratch_pg(tmp_path)
    admin = f"postgresql://ava@/postgres?host={sock}&port={port}"
    db_url = f"postgresql://ava@127.0.0.1:{port}/postgres"
    repl_url = f"postgresql://ava_pitr_repl:pw@127.0.0.1:{port}/postgres"
    subprocess.run(  # noqa: S603
        [pg_tool("psql"), admin, "-c", "CREATE ROLE ava_pitr_repl LOGIN REPLICATION PASSWORD 'pw'"],
        check=True,
        capture_output=True,
    )
    # Mirror prod: the renderer writes NO replication rows (initdb's defaults
    # still carry them) — every normal-connection probe then passes while a
    # physical replication connection would be refused.
    (data / "pg_hba.conf").write_text("local all all trust\nhost all all 127.0.0.1/32 trust\n")
    subprocess.run(  # noqa: S603
        [pg_tool("pg_ctl"), "-D", str(data), "reload"],
        check=True,
        capture_output=True,
    )
    try:
        monkeypatch.setattr("services.pitr.activation_runtime.pitr_admin_url", lambda: admin)
        with pytest.raises(BaseCandidateError, match="physical-replication rule"):
            _validate_replication_contract(db_url, repl_url)

        (data / "pg_hba.conf").write_text(
            "local all all trust\n"
            "host all all 127.0.0.1/32 trust\n"
            "host replication ava_pitr_repl 127.0.0.1/32 trust\n"
        )
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "reload"],
            check=True,
            capture_output=True,
        )
        _validate_replication_contract(db_url, repl_url)

        # A replication row with a per-line parse error is listed by
        # pg_hba_file_rules with a non-NULL error and is NOT in effect — the
        # preflight must treat it as absent (QA #1096 P2: the same-family blind
        # spot that ate the 2026-08-30 activation).
        (data / "pg_hba.conf").write_text(
            "local all all trust\n"
            "host all all 127.0.0.1/32 trust\n"
            "host replication ava_pitr_repl 127.0.0.1/32 bogus-method\n"
        )
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "reload"],
            check=True,
            capture_output=True,
        )
        with pytest.raises(BaseCandidateError, match="physical-replication rule"):
            _validate_replication_contract(db_url, repl_url)

        (data / "pg_hba.conf").write_text(
            "local all all trust\n"
            "host all all 127.0.0.1/32 trust\n"
            "host replication ava_pitr_repl 127.0.0.1/32 trust\n"
        )
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "reload"],
            check=True,
            capture_output=True,
        )
        _validate_replication_contract(db_url, repl_url)
    finally:
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "stop", "-m", "fast"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)
