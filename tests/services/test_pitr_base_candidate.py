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

# ─── stderr diagnostics ──────────────────────────────────────────────────────


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
