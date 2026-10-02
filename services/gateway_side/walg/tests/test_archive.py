"""The archive settings Postgres is launched with, and proof that Postgres can run them.

The unit tests pin the argument list. The integration tests launch a real
Postgres with exactly `archive_pg_args()` on its command line and a stand-in
`wal-g`, and check what only a real Postgres can: the quoting survives a home
path with a space and a `%p`, `%p` expands relative to the data directory, a
closed segment arrives in the store, and no secret reaches a place Postgres or
`ps` could show it.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from services.gateway_side.walg import archive
from services.gateway_side.walg.tests.support import (
    SECRETS,
    Sandbox,
    archiving_postgres,
    make_sandbox,
    wait_for,
    write_some_wal_and_switch,
)


def test_off_by_default_means_no_launch_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    assert archive.expected_archive() is None
    assert archive.archive_pg_args() == []


def test_the_launch_arguments_are_the_expected_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)

    args = archive.archive_pg_args()

    expected = archive.expected_archive()
    assert expected is not None
    assert args == [
        "-c",
        "archive_mode=on",
        "-c",
        "archive_timeout=60s",
        "-c",
        f"archive_command={expected.command}",
    ]
    assert (expected.mode, expected.timeout_s) == ("on", 60)
    argv = shlex.split(expected.command)
    assert argv == [
        str(sandbox.home).replace("%", "%%") + "/runtime/walg/wal-g",
        "--config",
        str(sandbox.config_file).replace("%", "%%"),
        "wal-push",
        "%p",
    ]


def test_a_percent_in_a_path_is_escaped_for_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_sandbox(tmp_path, monkeypatch)

    command = archive.archive_pg_args()[-1]

    assert "ava home%%p" in command
    assert command.count("%p") == 3, "two escaped path occurrences and the one real placeholder"
    assert command.endswith(" %p")


def test_no_argument_carries_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_sandbox(tmp_path, monkeypatch)

    joined = "\n".join(archive.archive_pg_args())

    assert not any(secret in joined for secret in SECRETS)


# ── a real Postgres ──────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def test_postgres_runs_the_archive_command_and_ships_a_closed_segment(sandbox: Sandbox) -> None:
    expected = archive.expected_archive()
    assert expected is not None

    with archiving_postgres() as pg, pg.connect() as conn:
        assert conn.execute("SHOW archive_mode").fetchone() == ("on",)
        assert conn.execute("SHOW archive_command").fetchone() == (expected.command,)
        assert conn.execute(
            "SELECT setting::int FROM pg_settings WHERE name = 'archive_timeout'"
        ).fetchone() == (archive.ARCHIVE_TIMEOUT_S,)

        write_some_wal_and_switch(conn)

        def archived() -> bool:
            row = conn.execute("SELECT archived_count FROM pg_stat_archiver").fetchone()
            return row is not None and row[0] >= 1

        wait_for(archived, what="the first WAL segment to be archived")
        failed = conn.execute("SELECT failed_count FROM pg_stat_archiver").fetchone()
        assert failed == (0,)

        stored = sandbox.stored()
        assert stored and all(len(name) == 24 for name in stored)
        assert (sandbox.store_dir / "store" / stored[0]).stat().st_size == 16 * 1024 * 1024
        first_call = sandbox.calls()[0]
        assert first_call == f"wal-push pg_wal/{stored[0]}", "%p is relative to the data directory"

        for name in ("postgresql.conf", "postgresql.auto.conf"):
            text = (pg.data / name).read_text()
            assert "wal-g" not in text, "archive settings are launch arguments, never written"
            assert not any(secret in text for secret in SECRETS)
        assert not any(secret in (pg.root / "pg.log").read_text() for secret in SECRETS)
        assert not any(secret in expected.command for secret in SECRETS)


def test_a_failing_archive_command_is_visible_and_never_blocks_writes(sandbox: Sandbox) -> None:
    sandbox.set_mode("fail")

    with archiving_postgres() as pg, pg.connect() as conn:
        write_some_wal_and_switch(conn)

        def failing() -> bool:
            row = conn.execute(
                "SELECT coalesce(last_failed_time > coalesce(last_archived_time, '-infinity'), false) "
                "FROM pg_stat_archiver"
            ).fetchone()
            return row == (True,)

        wait_for(failing, what="the archiver to report a failure")
        conn.execute("INSERT INTO walg_probe VALUES (-1)")  # writes keep working
        assert sandbox.stored() == []
