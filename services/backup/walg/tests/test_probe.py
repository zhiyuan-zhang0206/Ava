"""The archive health probe: states not thresholds, fixed texts, and a real Postgres to read."""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.config import settings
from services.backup.walg import config as walg_config
from services.backup.walg import probe
from services.backup.walg import state as walg_state
from services.backup.walg.archive import ARCHIVE_TIMEOUT_S, RPO_OBJECTIVE_S, expected_archive
from services.backup.walg.tests.support import (
    SECRETS,
    PgInstance,
    Sandbox,
    archiving_postgres,
    make_sandbox,
    wait_for,
    write_some_wal_and_switch,
)

_ALL_TEXTS = (
    probe.CONFIG_UNUSABLE,
    probe.KEY_CHANGED,
    probe.SETTINGS_DIFFER,
    probe.ARCHIVER_FAILING,
    probe.ARCHIVE_BEHIND,
    probe.UNREADABLE,
    probe.TICK_NOT_RUNNING,
    probe.CHAIN_BROKEN,
    probe.TICK_STATE_UNREADABLE,
    probe.RUN_FAILED,
    probe.DRILL_FAILED,
    probe.DRILL_OVERDUE,
    *probe.RUN_FAILED_AT.values(),
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _healthy_state() -> probe.ArchiverState:
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None
    return probe.ArchiverState(
        archive_mode=expected.mode,
        archive_command=expected.command,
        archive_timeout_s=expected.timeout_s,
        failing_now=False,
        archived_count=10,
        failed_count=0,
        oldest_pending_age_s=None,
    )


def test_failure_texts_carry_no_number_that_could_change_between_runs() -> None:
    """The probe's episode logic keys on the full message text: a changing number would
    make every run a new episode that never escalates."""
    for text in _ALL_TEXTS:
        assert not any(char.isdigit() for char in text), text


# ── judging a state ──────────────────────────────────────────────────────────


def test_a_matching_quiet_archiver_is_healthy(sandbox: Sandbox) -> None:
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None

    assert probe.judge(_healthy_state(), expected) is None


@pytest.mark.parametrize(
    "drift",
    [
        {"archive_mode": "off"},
        {"archive_timeout_s": ARCHIVE_TIMEOUT_S + 1},
        {"archive_command": "/old/path/wal-g --config /old.json wal-push %p"},
    ],
)
def test_postgres_running_other_archive_settings_is_reported(
    sandbox: Sandbox, drift: dict[str, Any]
) -> None:
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None

    assert probe.judge(replace(_healthy_state(), **drift), expected) == probe.SETTINGS_DIFFER


def test_a_failing_archiver_is_reported(sandbox: Sandbox) -> None:
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None

    state = replace(_healthy_state(), failing_now=True)

    assert probe.judge(state, expected) == probe.ARCHIVER_FAILING


def test_pending_wal_is_late_only_beyond_the_rpo_objective(sandbox: Sandbox) -> None:
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None
    healthy = _healthy_state()

    assert probe.judge(replace(healthy, oldest_pending_age_s=0.0), expected) is None
    assert probe.judge(replace(healthy, oldest_pending_age_s=RPO_OBJECTIVE_S), expected) is None
    assert (
        probe.judge(replace(healthy, oldest_pending_age_s=RPO_OBJECTIVE_S + 1), expected)
        == probe.ARCHIVE_BEHIND
    )


def test_settings_drift_outranks_the_archiver_conditions(sandbox: Sandbox) -> None:
    """An archiver that is off cannot be 'failing' or 'late': the settings are the cause."""
    expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
    assert expected is not None
    state = replace(
        _healthy_state(), archive_mode="off", failing_now=True, oldest_pending_age_s=10_000.0
    )

    assert probe.judge(state, expected) == probe.SETTINGS_DIFFER


# ── the files: configuration and key ─────────────────────────────────────────


def test_off_is_silent_and_never_dials_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    @contextmanager
    def explode() -> Generator[psycopg.Connection[Any]]:
        raise AssertionError("the probe dialed Postgres while WAL-G is off")
        yield  # pragma: no cover

    monkeypatch.setattr(probe, "admin_connection", explode)

    assert probe.failure(path_reader=lambda: settings.walg.walg_config_file) is None
    assert probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file) is None


def test_a_valid_configuration_is_fine(sandbox: Sandbox) -> None:
    assert probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file) is None


def test_an_unusable_configuration_is_reported_without_its_contents(sandbox: Sandbox) -> None:
    sandbox.config_file.write_text("{" + "".join(SECRETS))

    assert (
        probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file)
        == probe.CONFIG_UNUSABLE
    )


def test_a_swapped_key_is_reported_once_a_key_is_pinned(sandbox: Sandbox) -> None:
    sandbox.key_file.write_text("cd" * 32 + "\n")
    assert (
        probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file) is None
    ), "nothing is pinned yet: reading never pins"

    walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)
    assert probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file) is None

    sandbox.key_file.write_text("ef" * 32 + "\n")
    assert (
        probe.configuration_failure(path_reader=lambda: settings.walg.walg_config_file)
        == probe.KEY_CHANGED
    )


def test_the_probe_never_raises_when_postgres_cannot_be_read(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def unreachable() -> Generator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(probe, "admin_connection", unreachable)

    assert probe.failure(path_reader=lambda: settings.walg.walg_config_file) == probe.UNREADABLE


# ── a real Postgres ──────────────────────────────────────────────────────────


def _dial(pg: PgInstance, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def admin() -> Generator[psycopg.Connection[Any]]:
        with pg.connect() as conn:
            yield conn

    monkeypatch.setattr(probe, "admin_connection", admin)


def test_a_healthy_archiver_reads_healthy_and_an_idle_one_is_not_late(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    with archiving_postgres() as pg, pg.connect() as conn:
        _dial(pg, monkeypatch)
        write_some_wal_and_switch(conn)

        def settled() -> bool:
            state = probe.read_archiver_state(conn)
            return state.archived_count >= 1 and state.oldest_pending_age_s is None

        wait_for(settled, what="the closed segment to be archived")
        monkeypatch.setattr(probe, "RPO_OBJECTIVE_S", 1)
        time.sleep(2.5)  # idle for longer than the (shrunk) objective: nothing is pending

        state = probe.read_archiver_state(conn)
        expected = expected_archive(path_reader=lambda: settings.walg.walg_config_file)
        assert expected is not None
        assert (state.archive_mode, state.archive_command) == ("on", expected.command)
        assert (state.archive_timeout_s, state.failing_now, state.failed_count) == (
            ARCHIVE_TIMEOUT_S,
            False,
            0,
        )
        assert probe.judge(state, expected) is None
        assert probe.failure(path_reader=lambda: settings.walg.walg_config_file) is None


def test_a_failing_archive_command_is_reported_with_work_waiting(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.set_mode("fail")
    with archiving_postgres() as pg, pg.connect() as conn:
        _dial(pg, monkeypatch)
        write_some_wal_and_switch(conn)

        wait_for(lambda: probe.read_archiver_state(conn).failing_now, what="a failure")

        state = probe.read_archiver_state(conn)
        assert state.oldest_pending_age_s is not None, "the failed segment stays queued"
        assert (
            probe.failure(path_reader=lambda: settings.walg.walg_config_file)
            == probe.ARCHIVER_FAILING
        )


def test_a_hung_archive_command_is_caught_by_the_age_of_the_waiting_wal(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that neither succeeds nor fails leaves `failing_now` false: only the
    age of the queued segment shows it."""
    sandbox.set_mode("hang")
    with archiving_postgres() as pg, pg.connect() as conn:
        _dial(pg, monkeypatch)
        monkeypatch.setattr(probe, "RPO_OBJECTIVE_S", 2)
        write_some_wal_and_switch(conn)

        wait_for(
            lambda: probe.read_archiver_state(conn).oldest_pending_age_s is not None,
            what="the closed segment to be queued",
        )
        assert probe.failure(path_reader=lambda: settings.walg.walg_config_file) is None, (
            "freshly queued work is not late yet"
        )

        wait_for(
            lambda: (
                probe.failure(path_reader=lambda: settings.walg.walg_config_file)
                == probe.ARCHIVE_BEHIND
            ),
            seconds=30,
            what="the segment to be late",
        )
        state = probe.read_archiver_state(conn)
        assert state.failing_now is False
        sandbox.set_mode("ok")


def test_the_application_login_cannot_list_the_archive_queue(sandbox: Sandbox) -> None:
    """Why the probe dials the administrator socket: the unprivileged login the other
    health checks use is refused the queue listing, the only way to see how long a
    segment has been waiting."""
    with archiving_postgres() as pg:
        with pg.connect() as admin:
            admin.execute("CREATE ROLE app LOGIN")
        with (
            psycopg.connect(
                host=str(pg.root), port=pg.port, user="app", dbname="postgres", autocommit=True
            ) as app,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            app.execute("SELECT count(*) FROM pg_ls_archive_statusdir()")


def test_the_admin_dial_is_custody_checked_and_bounded(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe reaches the postmaster only through the home's owner-only admin
    socket, bound to its own data directory, with a connect timeout and a statement timeout."""
    from base.db import pg_admin

    seen: dict[str, Any] = {}

    class _Conn:
        def execute(self, statement: str) -> None:
            seen["statement"] = statement

    @contextmanager
    def connect(url: str, **kwargs: Any) -> Generator[_Conn]:
        seen["url"], seen["kwargs"] = url, kwargs
        yield _Conn()

    authority = pg_admin.OwnerAuthority(
        admin_url="postgresql://zzy@/postgres?host=/sockets/ava-pg-home&port=5433",
        database="db",
        owner="owner",
        data_dir=sandbox.home / "pg",
    )
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: authority)
    monkeypatch.setattr(pg_admin, "connect", connect)

    with probe.admin_connection():
        pass

    assert seen["url"] == authority.admin_url
    assert seen["kwargs"] == {
        "expected_data_dir": sandbox.home / "pg",
        "autocommit": True,
        "connect_timeout": probe._CONNECT_TIMEOUT_S,
    }
    assert seen["statement"].startswith("SET statement_timeout")


# ── the daily tick: A5 (run failed), A6 (tick not running), A7 (chain broken) ─

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def _run(status: walg_state.RunStatus = "ok", step: str | None = None) -> walg_state.RunRecord:
    return walg_state.RunRecord(
        started_at=NOW - timedelta(hours=1),
        finished_at=NOW - timedelta(minutes=30),
        status=status,
        step=step,
        detail="d",
    )


def _tick(age: timedelta, skipped: str | None = None) -> walg_state.TickRecord:
    return walg_state.TickRecord(started_at=NOW - age, skipped=skipped)


def _verify(integrity: str, timeline: str = "OK") -> walg_state.VerifyRecord:
    return walg_state.VerifyRecord(at=NOW, integrity=integrity, timeline=timeline)


def _judge(recorded: walg_state.State, *, since: datetime | None = None) -> str | None:
    return probe.tick_judge(recorded, NOW, enabled_since=since)


def test_a_recent_successful_tick_is_healthy() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), run=_run(), verify=_verify("OK"))

    assert _judge(recorded) is None


def test_a_warning_from_wal_verify_is_not_a_chain_failure() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), verify=_verify("WARNING"))

    assert _judge(recorded) is None


@pytest.mark.parametrize(("integrity", "timeline"), [("FAILURE", "OK"), ("OK", "FAILURE")])
def test_a_broken_chain_is_reported_whichever_check_found_it(integrity: str, timeline: str) -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)), run=_run(), verify=_verify(integrity, timeline)
    )

    assert _judge(recorded) == probe.CHAIN_BROKEN


def test_a_chain_failure_outranks_the_failed_run_it_caused() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)),
        run=_run("failed", walg_state.STEP_VERIFY),
        verify=_verify("FAILURE"),
    )

    assert _judge(recorded) == probe.CHAIN_BROKEN


@pytest.mark.parametrize("step", walg_state.STEPS)
def test_a_failed_run_is_reported_with_its_step(step: str) -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=1)), run=_run("failed", step))

    assert _judge(recorded) == probe.RUN_FAILED_AT[step]


def test_every_step_has_its_own_text() -> None:
    assert set(probe.RUN_FAILED_AT) == set(walg_state.STEPS)
    assert len(set(probe.RUN_FAILED_AT.values())) == len(walg_state.STEPS)


def test_a_failed_run_stays_reported_while_later_ticks_only_skip() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1), skipped="a deploy window is open"),
        run=_run("failed", walg_state.STEP_BACKUP),
    )

    assert _judge(recorded) == probe.RUN_FAILED_AT[walg_state.STEP_BACKUP]


def test_a_tick_that_started_within_a_period_is_running() -> None:
    just_inside = timedelta(hours=24) - timedelta(seconds=1)

    assert _judge(walg_state.State(tick=_tick(just_inside))) is None
    assert _judge(walg_state.State(tick=_tick(timedelta(hours=24)))) is None


def test_no_tick_for_longer_than_a_period_is_reported() -> None:
    just_outside = timedelta(hours=24) + timedelta(seconds=1)

    assert _judge(walg_state.State(tick=_tick(just_outside))) == probe.TICK_NOT_RUNNING


def test_a_skipped_tick_counts_as_a_tick_that_ran() -> None:
    recorded = walg_state.State(tick=_tick(timedelta(hours=2), skipped="postgres is down"))

    assert _judge(recorded) is None


def test_before_any_tick_the_time_since_enabling_is_what_counts() -> None:
    empty = walg_state.State()

    assert _judge(empty) is None, "no tick and no enabling time: nothing to compare"
    assert _judge(empty, since=NOW - timedelta(hours=23)) is None
    assert _judge(empty, since=NOW - timedelta(hours=25)) == probe.TICK_NOT_RUNNING


def test_the_enabling_time_is_the_key_pins_mtime(sandbox: Sandbox) -> None:
    walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)
    assert probe._enabled_since() is not None
    pinned_at = datetime(2026, 10, 1, 0, 0, tzinfo=UTC).timestamp()
    os.utime(walg_config.key_id_path(), (pinned_at, pinned_at))

    assert probe.tick_failure(now=datetime(2026, 10, 3, 0, 0, tzinfo=UTC)) == probe.TICK_NOT_RUNNING
    assert probe.tick_failure(now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC)) is None


def test_without_a_pin_there_is_no_enabling_time(sandbox: Sandbox) -> None:
    assert probe._enabled_since() is None
    assert probe.tick_failure(now=NOW) is None


def test_tick_failure_reads_the_state_file(sandbox: Sandbox) -> None:
    walg_state.write_state(
        walg_state.State(tick=_tick(timedelta(hours=1)), run=_run("failed", walg_state.STEP_VERIFY))
    )

    assert probe.tick_failure(now=NOW) == probe.RUN_FAILED_AT[walg_state.STEP_VERIFY]


def test_an_unreadable_state_file_is_its_own_fixed_failure_not_an_exception(
    sandbox: Sandbox,
) -> None:
    walg_state.state_path().parent.mkdir(parents=True)
    walg_state.state_path().write_text("{broken " + "".join(SECRETS))

    assert probe.tick_failure(now=NOW) == probe.TICK_STATE_UNREADABLE


# ── the recovery drill: A8 (latest failed) and "none succeeded in time" ─────


def _drill(
    *, ok: bool = True, last_ok_age: timedelta | None = timedelta(days=1)
) -> walg_state.DrillRecord:
    return walg_state.DrillRecord(
        finished_at=NOW - timedelta(hours=1),
        ok=ok,
        backup="base_1",
        target_lsn="0/A3000000",
        seconds=600.0,
        detail="d",
        last_ok_at=None if last_ok_age is None else NOW - last_ok_age,
    )


def _with_drill(drill: walg_state.DrillRecord | None) -> walg_state.State:
    return walg_state.State(tick=_tick(timedelta(hours=1)), drill=drill)


def test_a_drill_that_succeeded_within_its_period_is_healthy() -> None:
    assert _judge(_with_drill(_drill())) is None
    assert _judge(_with_drill(_drill(last_ok_age=timedelta(days=7)))) is None


def test_a_failed_latest_drill_is_reported_even_with_a_recent_earlier_success() -> None:
    assert _judge(_with_drill(_drill(ok=False))) == probe.DRILL_FAILED
    assert _judge(_with_drill(_drill(ok=False, last_ok_age=None))) == probe.DRILL_FAILED


def test_a_drill_is_overdue_one_tick_after_its_period() -> None:
    deadline = probe.DRILL_PERIOD + probe.TICK_PERIOD

    assert _judge(_with_drill(_drill(last_ok_age=deadline))) is None
    assert (
        _judge(_with_drill(_drill(last_ok_age=deadline + timedelta(seconds=1))))
        == probe.DRILL_OVERDUE
    )


def test_before_any_drill_the_time_since_enabling_counts() -> None:
    since = NOW - (probe.DRILL_PERIOD + probe.TICK_PERIOD + timedelta(seconds=1))

    assert _judge(_with_drill(None), since=since) == probe.DRILL_OVERDUE
    assert _judge(_with_drill(None), since=NOW - timedelta(days=3)) is None
    assert _judge(_with_drill(None), since=None) is None


def test_the_tick_conditions_outrank_the_drill_conditions() -> None:
    recorded = walg_state.State(
        tick=_tick(timedelta(hours=1)),
        run=_run("failed", walg_state.STEP_BACKUP),
        drill=_drill(ok=False),
    )

    assert _judge(recorded) == probe.RUN_FAILED_AT[walg_state.STEP_BACKUP]


def _healthy_archiver(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    @contextmanager
    def admin() -> Generator[None]:
        yield None

    def read(_conn: object) -> probe.ArchiverState:
        return replace(_healthy_state(), **changes)

    monkeypatch.setattr(probe, "admin_connection", admin)
    monkeypatch.setattr(probe, "read_archiver_state", read)


def test_failure_reports_the_tick_when_the_archiver_is_fine(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_archiver(monkeypatch)
    now = datetime.now(UTC)
    walg_state.write_state(
        walg_state.State(
            tick=walg_state.TickRecord(started_at=now),
            run=walg_state.RunRecord(
                started_at=now,
                finished_at=now,
                status="failed",
                step=walg_state.STEP_RETENTION,
                detail="d",
            ),
        )
    )

    assert (
        probe.failure(path_reader=lambda: settings.walg.walg_config_file)
        == probe.RUN_FAILED_AT[walg_state.STEP_RETENTION]
    )


def test_an_archiver_failure_outranks_a_tick_failure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_archiver(monkeypatch, failing_now=True)
    walg_state.write_state(
        walg_state.State(verify=_verify("FAILURE"), tick=walg_state.TickRecord(started_at=NOW))
    )

    assert (
        probe.failure(path_reader=lambda: settings.walg.walg_config_file) == probe.ARCHIVER_FAILING
    )


def test_off_does_not_read_the_tick_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("read the tick state while WAL-G is off")

    monkeypatch.setattr(walg_state, "read_state", explode)

    assert probe.failure(path_reader=lambda: settings.walg.walg_config_file) is None
