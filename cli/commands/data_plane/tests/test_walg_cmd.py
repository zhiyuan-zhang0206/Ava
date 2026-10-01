"""`ava backup walg check|status`: what an operator reads, and the parser that reaches it."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.config import settings
from cli.commands.data_plane import walg as walg_cmd
from cli.parsers import build_parser
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg import probe
from services.gateway_side.walg.tests.support import (
    SECRETS,
    PgInstance,
    Sandbox,
    archiving_postgres,
    make_sandbox,
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    sandbox = make_sandbox(tmp_path, monkeypatch)

    @contextmanager
    def no_postgres() -> Generator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(probe, "admin_connection", no_postgres)
    return sandbox


def test_the_parser_reaches_both_verbs() -> None:
    parser = build_parser()

    check = parser.parse_args(["backup", "walg", "check"])
    status = parser.parse_args(["backup", "walg", "status"])

    assert check.func.__name__ == "_h_backup_walg_check"
    assert status.func.__name__ == "_h_backup_walg_status"


def test_check_prints_a_mark_per_step_and_exits_zero_when_all_pass(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walg_cmd.cmd_walg_check() == 0

    out = capsys.readouterr().out
    assert [line.split(":")[0] for line in out.splitlines()] == [
        "  ✓ configured",
        "  ✓ binary",
        "  ✓ configuration",
        "  ✓ storage",
        "  ✓ postgres",
    ]
    assert not any(secret in out for secret in SECRETS)


def test_check_exits_non_zero_and_names_the_failing_step(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.set_mode("nodelete")

    assert walg_cmd.cmd_walg_check() == 1

    assert "  ✗ storage: cannot delete" in capsys.readouterr().out


def test_status_when_off_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    assert walg_cmd.cmd_walg_status() == 0

    assert capsys.readouterr().out == "WAL-G archiving is off (AVA_WALG_CONFIG_FILE is not set)\n"


def test_status_shows_the_fingerprint_not_the_key(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    fingerprint = walg_config.read_config(sandbox.config_file).key_fingerprint

    assert walg_cmd.cmd_walg_status() == 0

    out = capsys.readouterr().out
    assert f"key fingerprint: {fingerprint} (pinned: not yet)" in out
    assert "prefix: oss://ava-backups/ava-walg/test-home/pg17/gen1/" in out
    assert "postgres: archiver state not read" in out
    assert "health: WAL archiving: the archiver state is unreadable" in out
    assert not any(secret in out for secret in SECRETS)


def test_status_reports_an_unusable_configuration_instead_of_raising(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.config_file.write_text("{broken")

    assert walg_cmd.cmd_walg_status() == 0

    out = capsys.readouterr().out
    assert "configuration: UNUSABLE" in out
    assert "health: WAL archiving: the WAL-G configuration is unusable" in out


# ── the start-time warning for a retained postmaster ─────────────────────────


def _dial(pg: PgInstance, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def admin() -> Generator[psycopg.Connection[Any]]:
        with pg.connect() as conn:
            yield conn

    monkeypatch.setattr(probe, "admin_connection", admin)


def test_a_postgres_that_kept_its_old_launch_arguments_is_flagged_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The retained-postmaster case: archiving was configured after Postgres launched."""
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)
    with archiving_postgres() as pg:
        _dial(pg, monkeypatch)
        monkeypatch.setattr(settings.walg, "walg_config_file", sandbox.config_file)

        walg_cmd.warn_archive_inactive()

    err = capsys.readouterr().err
    assert "this Postgres is not running with the configured archive settings" in err
    assert "`ava stop` and `ava start`" in err


def test_a_postgres_launched_with_the_arguments_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        _dial(pg, monkeypatch)

        walg_cmd.warn_archive_inactive()

    assert capsys.readouterr().err == ""


def test_nothing_is_dialed_while_wal_g_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    def explode() -> object:
        raise AssertionError("dialed Postgres while WAL-G is off")

    monkeypatch.setattr(probe, "admin_connection", explode)

    walg_cmd.warn_archive_inactive()

    assert capsys.readouterr().err == ""


def test_an_unreadable_postgres_is_a_warning_not_a_failure(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    walg_cmd.warn_archive_inactive()

    assert "WAL archiving state not read (OperationalError" in capsys.readouterr().err
