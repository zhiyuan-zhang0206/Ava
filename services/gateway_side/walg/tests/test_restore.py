"""Restoring a WAL-G backup: a real Postgres, a base backup and WAL in the fake's store.

The source instance archives through `archive_pg_args()` into the fake's directory
(`tests/fake_walg.sh`), a `pg_basebackup` stands in for `backup-push`, and the code under
test fetches it back through the same fake and recovers it with a scratch postmaster.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest

from base.cluster.dataplane.pg_tools import pg_tool
from services.gateway_side.walg import restore
from services.gateway_side.walg.restore import RecoveryTarget, RestoreError, restored_instance
from services.gateway_side.walg.tests.support import (
    SECRETS,
    PgInstance,
    Sandbox,
    archive_current_segment,
    archiving_postgres,
    make_sandbox,
    take_basebackup,
)


@pytest.fixture
def socket_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The scratch socket directories this test's restores created (all must be removed)."""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(prefix: str, dir: str) -> str:
        path = real_mkdtemp(prefix=prefix, dir=dir)
        created.append(Path(path))
        return path

    monkeypatch.setattr(restore.tempfile, "mkdtemp", recording_mkdtemp)
    return created


def _scalar(conn: psycopg.Connection[Any], sql: str) -> Any:
    row = conn.execute(cast(LiteralString, sql)).fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True)
class Scenario:
    backup: str
    target: str  # right after B1b
    target_time: str  # after B1b committed, before B2 started
    b1_segment: str  # the archived segment holding B1


def _scenario(sandbox: Sandbox, pg: PgInstance) -> Scenario:
    """A table with A, then a backup, then B1 and B1b (the target is right after them), then B2.

    Every segment up to B2 is archived.
    """
    conn = pg.connect()
    conn.execute("CREATE TABLE t (k text)")
    conn.execute("INSERT INTO t VALUES ('A')")
    backup = take_basebackup(sandbox, pg, "base_000000010000000000000001")
    archive_current_segment(conn, sandbox)
    conn.execute("INSERT INTO t VALUES ('B1')")
    b1_segment = archive_current_segment(conn, sandbox)
    conn.execute("INSERT INTO t VALUES ('B1b')")
    target = str(_scalar(conn, "SELECT pg_current_wal_insert_lsn()"))
    target_time = str(_scalar(conn, "SELECT clock_timestamp()"))
    time.sleep(0.1)  # B2 commits strictly after the target time
    conn.execute("INSERT INTO t VALUES ('B2')")
    archive_current_segment(conn, sandbox)
    conn.close()
    return Scenario(backup, target, target_time, b1_segment)


def _restored_rows(instance: restore.RestoredInstance) -> list[str]:
    with psycopg.connect(
        host=str(instance.socket_dir), port=instance.port, user="ava", dbname="postgres"
    ) as conn:
        return [str(row[0]) for row in conn.execute("SELECT k FROM t ORDER BY k").fetchall()]


def _assert_scratch_posture(instance: restore.RestoredInstance, *, max_connections: str) -> None:
    with psycopg.connect(
        host=str(instance.socket_dir), port=instance.port, user="ava", dbname="postgres"
    ) as conn:
        assert _scalar(conn, "SHOW archive_mode") == "off"
        assert _scalar(conn, "SHOW listen_addresses") == ""
        assert _scalar(conn, "SHOW max_connections") == max_connections
        assert _scalar(conn, "SELECT NOT pg_is_in_recovery()") is True


def _assert_data_directory_left_behind(destination: Path, socket_dirs: list[Path]) -> None:
    """The product is a promoted data directory, its postmaster shut down cleanly."""
    assert (destination / "PG_VERSION").is_file()
    assert not (destination / "postmaster.pid").exists()
    assert not (destination / "recovery.signal").exists()
    assert socket_dirs
    assert not any(path.exists() for path in socket_dirs)


def test_recovers_to_the_target_lsn_and_never_touches_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_dirs: list[Path]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    # max_connections above the default: the recovering instance only starts if it
    # copies the capacity the backup's control file records.
    with archiving_postgres("-c", "max_connections=150") as pg:
        scenario = _scenario(sandbox, pg)
        stored_before = sandbox.stored()
        destination = tmp_path / "restored"
        reports: list[str] = []
        with restored_instance(
            destination,
            backup=scenario.backup,
            target=RecoveryTarget(lsn=scenario.target),
            report=reports.append,
            user="ava",
            keep_data=True,
        ) as instance:
            assert _restored_rows(instance) == ["A", "B1", "B1b"]
            _assert_scratch_posture(instance, max_connections="150")
            assert instance.data_dir == destination
        # nothing was archived by the recovering instance, the source kept its own data
        assert sandbox.stored() == stored_before
        assert _scalar(pg.connect(), "SELECT count(*) FROM t") == 4
    assert any("promoted on timeline 2" in line for line in reports)
    _assert_data_directory_left_behind(destination, socket_dirs)


def test_recovers_to_a_target_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        scenario = _scenario(sandbox, pg)
        with restored_instance(
            tmp_path / "restored",
            backup=scenario.backup,
            target=RecoveryTarget(time=scenario.target_time),
            report=lambda _line: None,
            user="ava",
            keep_data=False,
        ) as instance:
            assert _restored_rows(instance) == ["A", "B1", "B1b"]


def test_without_a_target_recovery_replays_all_archived_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        scenario = _scenario(sandbox, pg)
        with restored_instance(
            tmp_path / "restored",
            backup=scenario.backup,
            target=RecoveryTarget(),
            report=lambda _line: None,
            user="ava",
            keep_data=False,
        ) as instance:
            assert _restored_rows(instance) == ["A", "B1", "B1b", "B2"]


def test_a_backup_that_carries_archive_settings_still_cannot_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that once wrote archive settings into postgresql.auto.conf brings them back."""
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        scenario = _scenario(sandbox, pg)
        auto_conf = sandbox.store_dir / "basebackups" / scenario.backup / "postgresql.auto.conf"
        with auto_conf.open("a") as handle:
            handle.write("archive_mode = 'on'\n")
            handle.write(f"archive_command = '{sandbox.home}/runtime/walg/wal-g --config ")
            handle.write(f"{sandbox.config_file} wal-push %p'\n")
        stored_before = sandbox.stored()
        with (
            restored_instance(
                tmp_path / "restored",
                backup=scenario.backup,
                target=RecoveryTarget(lsn=scenario.target),
                report=lambda _line: None,
                user="ava",
                keep_data=False,
            ) as instance,
            psycopg.connect(
                host=str(instance.socket_dir), port=instance.port, user="ava", dbname="postgres"
            ) as conn,
        ):
            assert _scalar(conn, "SHOW archive_mode") == "off"
        assert sandbox.stored() == stored_before


def test_a_missing_segment_between_backup_and_target_fails_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_dirs: list[Path]
) -> None:
    """The target is only reachable through an unbroken chain: Postgres' own FATAL says so."""
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        scenario = _scenario(sandbox, pg)
        # drop the segment holding B1: it lies after the backup and before the target
        (sandbox.store_dir / "store" / scenario.b1_segment).unlink()
        destination = tmp_path / "restored"
        with (
            pytest.raises(RestoreError, match="recovery failed"),
            restored_instance(
                destination,
                backup=scenario.backup,
                target=RecoveryTarget(lsn=scenario.target),
                report=lambda _line: None,
                user="ava",
                keep_data=False,
            ),
        ):
            pytest.fail("recovery must not reach a target behind a gap")
    assert socket_dirs
    assert not any(path.exists() for path in socket_dirs)


def test_refuses_a_directory_that_is_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("x")
    with (
        pytest.raises(RestoreError, match="not an empty directory"),
        restored_instance(
            occupied,
            backup="LATEST",
            target=RecoveryTarget(),
            report=lambda _line: None,
            keep_data=True,
        ),
    ):
        pytest.fail("must not fetch into an occupied directory")
    assert not any("backup-fetch" in call for call in sandbox.calls())


def test_refuses_the_live_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    for directory in (sandbox.home / "pg", sandbox.home / "pg" / "sub", sandbox.home):
        with (
            pytest.raises(RestoreError, match="live data directory"),
            restored_instance(
                directory,
                backup="LATEST",
                target=RecoveryTarget(),
                report=lambda _line: None,
                keep_data=True,
            ),
        ):
            pytest.fail("must refuse")
    assert not (sandbox.home / "pg").exists()
    assert not any("backup-fetch" in call for call in sandbox.calls())


def test_a_failed_fetch_is_a_restore_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    sandbox.fail("backup-fetch")
    with (
        pytest.raises(RestoreError, match="backup-fetch failed"),
        restored_instance(
            tmp_path / "restored",
            backup="LATEST",
            target=RecoveryTarget(),
            report=lambda _line: None,
            keep_data=True,
        ),
    ):
        pytest.fail("must not start Postgres after a failed fetch")


def test_recovery_target_is_validated() -> None:
    assert RecoveryTarget().pg_args() == []
    assert RecoveryTarget(lsn="0/3000060").pg_args() == ["-c", "recovery_target_lsn=0/3000060"]
    assert RecoveryTarget(time="2026-10-01 12:04:57+00").pg_args() == [
        "-c",
        "recovery_target_time=2026-10-01 12:04:57+00",
    ]
    for bad in ({"lsn": "3000060"}, {"lsn": "0/3000060 --evil"}, {"time": " "}):
        with pytest.raises(ValueError):
            RecoveryTarget(**bad)
    with pytest.raises(ValueError, match="not both"):
        RecoveryTarget(time="2026-10-01", lsn="0/1")


def test_the_postmaster_command_line_holds_every_safety_setting_and_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)

    def fake_control_settings(_directory: Path) -> dict[str, str]:
        return {"max_connections": "500", "max_worker_processes": "8"}

    monkeypatch.setattr(restore, "control_settings", fake_control_settings)
    argv = restore.recovery_argv(
        tmp_path / "data",
        socket_dir=tmp_path / "sock",
        port=54329,
        target=RecoveryTarget(lsn="0/3000060"),
    )
    settings = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    command = next(s for s in settings if s.startswith("restore_command="))
    wanted = {
        "archive_mode=off",
        "listen_addresses=",
        "recovery_target_action=promote",
        "recovery_target_lsn=0/3000060",
        "max_connections=500",
        "max_worker_processes=8",
        f"unix_socket_directories={tmp_path / 'sock'}",
    }
    assert wanted <= set(settings)
    # the home path of the sandbox holds a literal "%p": Postgres must see "%%p"
    assert command.endswith("wal-fetch %f %p")
    assert "ava home%%p" in command
    assert sandbox.config_file.name in command
    assert argv[argv.index("-D") + 1] == str(tmp_path / "data")
    joined = " ".join(argv)
    assert "archive_command" not in joined
    assert not [secret for secret in SECRETS if secret in joined]


def test_control_settings_reads_what_the_control_file_records(tmp_path: Path) -> None:
    data = tmp_path / "data"
    subprocess.run(  # noqa: S603 — the resolved initdb with static flags
        [str(pg_tool("initdb")), "-D", str(data), "-U", "ava", "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
    )

    settings = restore.control_settings(data)

    assert settings["max_connections"] == "100"
    assert set(settings) == {
        "max_connections",
        "max_worker_processes",
        "max_wal_senders",
        "max_prepared_transactions",
        "max_locks_per_transaction",
    }


def test_control_settings_refuses_a_directory_without_a_control_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RestoreError, match="pg_controldata"):
        restore.control_settings(empty)
