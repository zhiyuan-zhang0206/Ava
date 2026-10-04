"""The state file: private, atomic, strict on read, and round-trip exact."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.backup.walg import state
from services.backup.walg.state import (
    BackupRecord,
    DrillRecord,
    RetentionRecord,
    RunRecord,
    State,
    StateError,
    TickRecord,
    VerifyRecord,
)
from services.backup.walg.tests.support import Sandbox, make_sandbox

T0 = datetime(2026, 10, 2, 6, 25, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 2, 7, 5, 30, tzinfo=UTC)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _full_state() -> State:
    return State(
        tick=TickRecord(started_at=T0, skipped=None),
        run=RunRecord(started_at=T0, finished_at=T1, status="failed", step="verify", detail="gap"),
        backup=BackupRecord(
            name="base_000000010000000000000087",
            kind="full",
            finished_at=T1,
            uncompressed_bytes=2452668013,
            compressed_bytes=288734812,
        ),
        verify=VerifyRecord(at=T1, integrity="WARNING", timeline="OK"),
        retention=RetentionRecord(at=T1, marked=21, deleted=21),
        drill=DrillRecord(
            finished_at=T1,
            ok=False,
            backup="base_000000010000000000000087",
            target_lsn="0/A3000000",
            seconds=612.5,
            detail="recovery failed",
            last_ok_at=T0,
        ),
    )


def test_no_file_is_an_empty_state(sandbox: Sandbox) -> None:
    assert state.read_state() == State()


def test_a_written_state_reads_back_exactly(sandbox: Sandbox) -> None:
    state.write_state(_full_state())

    assert state.read_state() == _full_state()


def test_the_file_is_owner_only_and_its_directory_private(sandbox: Sandbox) -> None:
    state.write_state(_full_state())

    path = state.state_path()
    assert path == sandbox.home / "backups" / "walg" / "state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_write_leaves_no_temporary_file(sandbox: Sandbox) -> None:
    state.write_state(_full_state())
    state.write_state(_full_state())

    assert [p.name for p in state.state_path().parent.iterdir()] == ["state.json"]


def test_timestamps_are_utc_text(sandbox: Sandbox) -> None:
    state.write_state(_full_state())

    raw = json.loads(state.state_path().read_text())
    assert raw["tick"]["started_at"] == "2026-10-02T06:25:00+00:00"


def test_update_replaces_only_the_named_sections(sandbox: Sandbox) -> None:
    state.write_state(_full_state())

    updated = state.update_state(verify=VerifyRecord(at=T0, integrity="OK", timeline="OK"))

    assert updated.verify == VerifyRecord(at=T0, integrity="OK", timeline="OK")
    assert updated.run == _full_state().run
    assert updated.drill == _full_state().drill
    assert state.read_state() == updated


def test_the_drill_section_is_carried_through_unchanged(sandbox: Sandbox) -> None:
    state.write_state(_full_state())

    state.update_state(tick=TickRecord(started_at=T1, skipped="deploy window"))

    assert state.read_state().drill == _full_state().drill


_DROP = object()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "version", 2),
        (None, "run", _DROP),
        ("run", "status", "running"),
        ("backup", "kind", "differential"),
        ("tick", "started_at", "yesterday"),
        ("tick", "started_at", "2026-10-02T06:25:00"),
        ("retention", "deleted", _DROP),
        ("verify", "at", None),
        ("drill", "ok", "yes"),
        ("drill", "seconds", _DROP),
    ],
    ids=[
        "version",
        "missing-section",
        "run-status",
        "backup-kind",
        "bad-timestamp",
        "naive-timestamp",
        "missing-field",
        "null-timestamp",
        "drill-ok-not-boolean",
        "drill-missing-field",
    ],
)
def test_a_malformed_file_is_an_error_never_an_empty_state(
    sandbox: Sandbox, section: str | None, field: str, value: object
) -> None:
    state.write_state(_full_state())
    raw: dict[str, Any] = json.loads(state.state_path().read_text())
    target: dict[str, Any] = raw if section is None else raw[section]
    if value is _DROP:
        del target[field]
    else:
        target[field] = value
    state.state_path().write_text(json.dumps(raw))

    with pytest.raises(StateError):
        state.read_state()


@pytest.mark.parametrize("content", ["", "not json", "[]", "null"])
def test_a_file_that_is_not_a_state_object_is_an_error(sandbox: Sandbox, content: str) -> None:
    state.state_path().parent.mkdir(parents=True)
    state.state_path().write_text(content)

    with pytest.raises(StateError):
        state.read_state()


def test_a_naive_timestamp_cannot_be_written(sandbox: Sandbox) -> None:
    naive = State(tick=TickRecord(started_at=datetime(2026, 10, 2, 6, 25)))  # noqa: DTZ001 — naive on purpose

    with pytest.raises(ValueError, match="timezone-aware"):
        state.write_state(naive)


def test_a_drill_without_a_target_or_a_success_round_trips(sandbox: Sandbox) -> None:
    record = DrillRecord(
        finished_at=T1,
        ok=True,
        backup="base_1",
        target_lsn=None,
        seconds=3.0,
        detail="ok",
        last_ok_at=T1,
    )
    never = replace(record, ok=False, last_ok_at=None)

    for drill in (record, never):
        state.write_state(State(drill=drill))
        assert state.read_state().drill == drill
