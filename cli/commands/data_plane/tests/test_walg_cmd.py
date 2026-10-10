"""`ava backup walg check|run|restore|status`: what an operator reads, and the parser that reaches it."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from base.config import ConfigBoot, settings
from cli.commands.data_plane import walg as walg_cmd
from cli.parsers import build_parser
from services.backup.walg import config as walg_config
from services.backup.walg import probe, state
from services.backup.walg.check import Step
from services.backup.walg.tests.support import (
    SECRETS,
    PgInstance,
    Sandbox,
    archiving_postgres,
    make_sandbox,
)


@pytest.fixture(autouse=True)
def config_boot_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Restore process delivery from each independent operation's boot."""

    def build_owner() -> ConfigBoot:
        config = ConfigBoot()
        config.view.walg.walg_config_file = settings.walg.walg_config_file
        return config

    monkeypatch.setattr(walg_cmd, "ConfigBoot", build_owner)
    with patch.dict(os.environ):
        yield


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    sandbox = make_sandbox(tmp_path, monkeypatch)

    @contextmanager
    def no_postgres() -> Generator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(probe, "admin_connection", no_postgres)
    return sandbox


def test_the_parser_reaches_every_verb() -> None:
    parser = build_parser()

    check = parser.parse_args(["backup", "walg", "check"])
    run = parser.parse_args(["backup", "walg", "run"])
    status = parser.parse_args(["backup", "walg", "status"])
    restore = parser.parse_args(["backup", "walg", "restore", "--dir", "/srv/restored"])
    drill = parser.parse_args(["backup", "walg", "drill"])

    assert drill.func.__name__ == "_h_backup_walg_drill"

    assert restore.func.__name__ == "_h_backup_walg_restore"
    assert (restore.dir, restore.backup, restore.time, restore.lsn, restore.user) == (
        "/srv/restored",
        "LATEST",
        None,
        None,
        None,
    )
    named = parser.parse_args(["backup", "walg", "restore", "--dir", "/d", "--user", "zyonzhang"])
    assert named.user == "zyonzhang"
    assert check.func.__name__ == "_h_backup_walg_check"
    assert run.func.__name__ == "_h_backup_walg_run"
    assert status.func.__name__ == "_h_backup_walg_status"


def test_run_hands_the_tick_a_timestamping_reporter_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_tick(_db: object, report: Any, **_inputs: object) -> int:
        report("skipped: postgres is not accepting connections")
        return 1

    monkeypatch.setattr(walg_cmd.tick, "run_tick", fake_tick)

    assert walg_cmd.cmd_walg_run() == 1

    out = capsys.readouterr().out
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z skipped: postgres is not accepting connections\n",
        out,
    )


def test_status_says_when_the_tick_never_ran(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walg_cmd.cmd_walg_status() == 0

    assert "daily tick: never ran" in capsys.readouterr().out


def test_status_shows_the_last_run_and_its_failed_step(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    started = datetime(2026, 10, 2, 6, 25, tzinfo=UTC)
    state.write_state(
        state.State(
            tick=state.TickRecord(started_at=started),
            run=state.RunRecord(
                started_at=started,
                finished_at=started,
                status="failed",
                step="verify",
                detail="the archived WAL chain is broken",
            ),
            verify=state.VerifyRecord(at=started, integrity="FAILURE", timeline="OK"),
        )
    )

    assert walg_cmd.cmd_walg_status() == 0

    out = capsys.readouterr().out
    assert "daily tick: last started 2026-10-02T06:25:00+00:00" in out
    assert "last run: failed at verify (2026-10-02T06:25:00+00:00): the archived WAL chain" in out
    assert "last verify: integrity FAILURE, timeline OK" in out


def test_status_reports_an_unreadable_state_file(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    state.state_path().parent.mkdir(parents=True)
    state.state_path().write_text("{broken")

    assert walg_cmd.cmd_walg_status() == 0

    assert "daily tick: state UNREADABLE" in capsys.readouterr().out


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

        walg_cmd.warn_archive_inactive(path_reader=lambda: settings.walg.walg_config_file)

    err = capsys.readouterr().err
    assert "this Postgres is not running with the configured archive settings" in err
    assert "`ava stop` and `ava start`" in err


def test_a_postgres_launched_with_the_arguments_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        _dial(pg, monkeypatch)

        walg_cmd.warn_archive_inactive(path_reader=lambda: settings.walg.walg_config_file)

    assert capsys.readouterr().err == ""


def test_nothing_is_dialed_while_wal_g_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    def explode() -> object:
        raise AssertionError("dialed Postgres while WAL-G is off")

    monkeypatch.setattr(probe, "admin_connection", explode)

    walg_cmd.warn_archive_inactive(path_reader=lambda: settings.walg.walg_config_file)

    assert capsys.readouterr().err == ""


def test_an_unreadable_postgres_is_a_warning_not_a_failure(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    walg_cmd.warn_archive_inactive(path_reader=lambda: settings.walg.walg_config_file)

    assert "WAL archiving state not read (OperationalError" in capsys.readouterr().err


@contextmanager
def _fake_restore(calls: list[dict[str, Any]], **kwargs: Any) -> Generator[None]:
    calls.append(kwargs)
    yield


def test_restore_passes_the_target_and_keeps_the_data_directory(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake(directory: Path, **kwargs: Any) -> Any:
        return _fake_restore(calls, directory=directory, **kwargs)

    monkeypatch.setattr(walg_cmd, "restored_instance", fake)

    code = walg_cmd.cmd_walg_restore(
        directory=str(tmp_path / "out"),
        backup="base_0001",
        time=None,
        lsn="0/3000060",
        user="zyonzhang",
    )

    assert code == 0
    (call,) = calls
    assert call["directory"] == (tmp_path / "out").resolve()
    assert call["backup"] == "base_0001"
    assert call["target"].lsn == "0/3000060"
    assert call["keep_data"] is True
    assert call["user"] == "zyonzhang"
    assert "Postgres is not running on it" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"time": None, "lsn": "not-an-lsn"}, "is not an LSN"),
        ({"time": "2026-10-01", "lsn": "0/1"}, "not both"),
    ],
)
def test_restore_rejects_a_bad_target_before_fetching(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kwargs: dict[str, str | None],
    message: str,
) -> None:
    code = walg_cmd.cmd_walg_restore(directory=str(tmp_path / "out"), backup="LATEST", **kwargs)  # type: ignore[arg-type]

    assert code == 1
    assert message in capsys.readouterr().err
    assert not sandbox.calls()


def test_restore_reports_a_failed_recovery_and_exits_non_zero(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.backup.walg.restore import RestoreError

    @contextmanager
    def failing(*_args: Any, **_kwargs: Any) -> Generator[None]:
        raise RestoreError("recovery failed, postgres exited 1: FATAL: no segment")
        yield  # pragma: no cover

    monkeypatch.setattr(walg_cmd, "restored_instance", failing)

    code = walg_cmd.cmd_walg_restore(
        directory="/srv/restored", backup="LATEST", time=None, lsn=None
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "restore failed: recovery failed" in err
    assert "remove any partly restored content" in err


def test_restore_while_wal_g_is_off_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    assert walg_cmd.cmd_walg_restore(directory="/x", backup="LATEST", time=None, lsn=None) == 1
    assert "WAL-G is off" in capsys.readouterr().out


def test_drill_hands_the_tick_module_a_timestamping_reporter_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_drill(report: Any, **_inputs: object) -> int:
        report("drill: FAILED after 3s: recovery failed")
        return 1

    monkeypatch.setattr(walg_cmd.tick, "run_drill_now", fake_drill)

    assert walg_cmd.cmd_walg_drill() == 1
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z drill: FAILED after 3s: recovery failed\n",
        capsys.readouterr().out,
    )


def test_status_shows_the_last_drill_and_the_last_success(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    stamp = datetime(2026, 10, 2, 6, 25, tzinfo=UTC)
    state.write_state(
        state.State(
            tick=state.TickRecord(started_at=stamp),
            drill=state.DrillRecord(
                finished_at=stamp,
                ok=False,
                backup="base_000000010000000000000087",
                target_lsn="0/A3000000",
                seconds=612.4,
                detail="recovery failed: no segment",
                last_ok_at=None,
            ),
        )
    )

    assert walg_cmd.cmd_walg_status() == 0

    out = capsys.readouterr().out
    assert (
        "last drill: FAILED (2026-10-02T06:25:00+00:00): base_000000010000000000000087 to 0/A3000000 in 612s; recovery failed: no segment"
        in out
    )
    assert "last successful drill: never" in out


def test_check_uses_one_lazy_owner_and_keeps_its_reader_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = ConfigBoot()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    owner.view.walg.walg_config_file = first
    owners: list[ConfigBoot] = []

    def build_owner() -> ConfigBoot:
        owners.append(owner)
        return owner

    def check_reader(*, path_reader: Callable[[], Path | None]) -> list[Step]:
        assert path_reader() == first
        owner.view.walg.walg_config_file = second
        assert path_reader() == second
        return []

    def no_boot(_owner: ConfigBoot) -> None:
        pytest.fail("a CLI root must not eagerly boot its reader")

    monkeypatch.setattr(walg_cmd, "ConfigBoot", build_owner)
    monkeypatch.setattr(ConfigBoot, "boot", no_boot)
    monkeypatch.setattr(walg_cmd.check, "run_check", check_reader)
    monkeypatch.setattr(settings.walg, "walg_config_file", tmp_path / "ambient.json")
    assert walg_cmd.cmd_walg_check() == 0
    assert owners == [owner]
