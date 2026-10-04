"""`ava backup walg check`: every way setup can be wrong is a named failing step."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.dataplane import walg_binary
from services.backup.walg import check, probe
from services.backup.walg import config as walg_config
from services.backup.walg.tests.support import (
    SECRETS,
    Sandbox,
    archiving_postgres,
    make_sandbox,
    valid_config,
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


def _verdicts(steps: list[check.Step]) -> list[tuple[str, bool]]:
    return [(step.name, step.ok) for step in steps]


def test_off_is_a_single_failing_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    steps = check.run_check()

    assert _verdicts(steps) == [("configured", False)]
    assert "AVA_WALG_CONFIG_FILE is not set" in steps[0].detail


def test_a_working_setup_passes_every_step_and_leaves_nothing_in_the_store(
    sandbox: Sandbox,
) -> None:
    steps = check.run_check()

    assert _verdicts(steps) == [
        ("configured", True),
        ("binary", True),
        ("configuration", True),
        ("storage", True),
        ("postgres", True),
    ]
    assert sandbox.stored() == [], "the check deletes what it wrote"
    kinds = [call.split()[0:2] for call in sandbox.calls()]
    assert kinds == [["st", "put"], ["st", "ls"], ["st", "get"], ["st", "rm"], ["st", "ls"]]
    put = sandbox.calls()[0].split()
    assert put[3].startswith("ava-check/") and put[3].endswith(".txt")
    assert sandbox.calls()[2].split()[2] == put[3] + ".lz4", "`st get` takes the stored .lz4 name"
    assert "not pinned yet" in next(s.detail for s in steps if s.name == "configuration")
    assert not any(secret in step.detail for step in steps for secret in SECRETS)


def test_the_check_never_pins_the_key(sandbox: Sandbox) -> None:
    check.run_check()

    assert walg_config.pinned_key_id() is None


def test_a_missing_or_edited_binary_stops_at_the_binary(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(walg_binary, "installed_problem", lambda: "wal-g is not installed at /x")

    steps = check.run_check()

    assert _verdicts(steps) == [("configured", True), ("binary", False)]
    assert "ava converge installs it" in steps[-1].detail
    assert sandbox.calls() == []


def test_an_unusable_configuration_stops_before_any_storage_call(sandbox: Sandbox) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_PREVENT_WAL_OVERWRITE=None))

    steps = check.run_check()

    assert _verdicts(steps)[-1] == ("configuration", False)
    assert "WALG_PREVENT_WAL_OVERWRITE" in steps[-1].detail
    assert sandbox.calls() == []


def test_a_key_that_differs_from_the_pinned_one_fails(sandbox: Sandbox) -> None:
    walg_config.load_walg_config()
    sandbox.key_file.write_text("cd" * 32 + "\n")

    steps = check.run_check()

    assert _verdicts(steps)[-1] == ("configuration", False)
    assert "is not the key this home pinned" in steps[-1].detail
    assert sandbox.calls() == []


def test_unreachable_storage_fails_the_storage_step(sandbox: Sandbox) -> None:
    sandbox.set_mode("fail")

    steps = check.run_check()

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "simulated failure" in steps[-1].detail


def test_a_credential_that_cannot_delete_fails_even_though_it_can_write(sandbox: Sandbox) -> None:
    sandbox.set_mode("nodelete")

    steps = check.run_check()

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "cannot delete" in steps[-1].detail and "Delete granted" in steps[-1].detail


def test_a_round_trip_that_returns_other_bytes_fails(sandbox: Sandbox) -> None:
    sandbox.set_mode("corrupt")

    steps = check.run_check()

    assert _verdicts(steps)[-1] == ("storage", False)
    assert "differs from the one written" in steps[-1].detail
    assert sandbox.stored() == [], "a failed check still cleans up after itself"


def test_postgres_facts_are_reported_not_judged(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = check.run_check()
    assert steps[-1].ok and "not read" in steps[-1].detail

    with archiving_postgres() as pg:

        @contextmanager
        def admin() -> Generator[psycopg.Connection[Any]]:
            with pg.connect() as conn:
                yield conn

        monkeypatch.setattr(probe, "admin_connection", admin)
        running = check.run_check()[-1]

    assert running.ok
    assert "archive_mode=on, matches the configuration" in running.detail
