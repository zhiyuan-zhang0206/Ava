"""The archive health probe: states not thresholds, fixed texts, and a real Postgres to read."""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg import probe
from services.gateway_side.walg.archive import ARCHIVE_TIMEOUT_S, RPO_OBJECTIVE_S, expected_archive
from services.gateway_side.walg.tests.support import (
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
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _healthy_state() -> probe.ArchiverState:
    expected = expected_archive()
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
    expected = expected_archive()
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
    expected = expected_archive()
    assert expected is not None

    assert probe.judge(replace(_healthy_state(), **drift), expected) == probe.SETTINGS_DIFFER


def test_a_failing_archiver_is_reported(sandbox: Sandbox) -> None:
    expected = expected_archive()
    assert expected is not None

    state = replace(_healthy_state(), failing_now=True)

    assert probe.judge(state, expected) == probe.ARCHIVER_FAILING


def test_pending_wal_is_late_only_beyond_the_rpo_objective(sandbox: Sandbox) -> None:
    expected = expected_archive()
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
    expected = expected_archive()
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

    assert probe.failure() is None
    assert probe.configuration_failure() is None


def test_a_valid_configuration_is_fine(sandbox: Sandbox) -> None:
    assert probe.configuration_failure() is None


def test_an_unusable_configuration_is_reported_without_its_contents(sandbox: Sandbox) -> None:
    sandbox.config_file.write_text("{" + "".join(SECRETS))

    assert probe.configuration_failure() == probe.CONFIG_UNUSABLE


def test_a_swapped_key_is_reported_once_a_key_is_pinned(sandbox: Sandbox) -> None:
    sandbox.key_file.write_text("cd" * 32 + "\n")
    assert probe.configuration_failure() is None, "nothing is pinned yet: reading never pins"

    walg_config.load_walg_config()
    assert probe.configuration_failure() is None

    sandbox.key_file.write_text("ef" * 32 + "\n")
    assert probe.configuration_failure() == probe.KEY_CHANGED


def test_the_probe_never_raises_when_postgres_cannot_be_read(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def unreachable() -> Generator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(probe, "admin_connection", unreachable)

    assert probe.failure() == probe.UNREADABLE


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
        expected = expected_archive()
        assert expected is not None
        assert (state.archive_mode, state.archive_command) == ("on", expected.command)
        assert (state.archive_timeout_s, state.failing_now, state.failed_count) == (
            ARCHIVE_TIMEOUT_S,
            False,
            0,
        )
        assert probe.judge(state, expected) is None
        assert probe.failure() is None


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
        assert probe.failure() == probe.ARCHIVER_FAILING


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
        assert probe.failure() is None, "freshly queued work is not late yet"

        wait_for(
            lambda: probe.failure() == probe.ARCHIVE_BEHIND,
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
