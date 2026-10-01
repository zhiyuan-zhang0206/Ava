"""Retention: nothing is deleted unless the audited dry run is structurally safe.

`fixtures/retention-dry-run.log` is what a real WAL-G v3.0.9 printed for `delete retain
FULL 2 --use-sentinel-time` over `fixtures/backup-list.json` (three full backups and an
increment of the first): it marks the oldest full backup, its increment and the WAL
before the second full backup. The WAL lines are cut to a handful of the 543 and the
count recomputed; every other line is verbatim. The tests that drive `apply_retention`
add a newer full backup so that more than the retained number exist.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from services.gateway_side.walg import retention
from services.gateway_side.walg.backups import Backup, parse_backups
from services.gateway_side.walg.retention import (
    RetentionAbortedError,
    RetentionOutcome,
    apply_retention,
    assert_invariants,
    parse_marked,
)
from services.gateway_side.walg.runner import WalgCommandError, WalgOutput
from services.gateway_side.walg.tests.support import Sandbox, fixture_text, make_sandbox

FULL_1 = "base_000000010000000000000087"
DELTA_OF_1 = "base_0000000100000000000000A6_D_000000010000000000000087"
FULL_2 = "base_000000010000000200000020"
FULL_3 = "base_0000000100000004000000B6"
FULL_4 = "base_0000000100000006000000C0"

DRY_RUN = fixture_text("retention-dry-run.log")
PLANNED = parse_marked(WalgOutput(stdout="", stderr=DRY_RUN), retention._DRY_RUN_COUNT)


def _confirm_log() -> str:
    """The log of the same run with `--confirm`: the same lines, a different last one."""
    lines = [line for line in DRY_RUN.splitlines() if "Dry run:" not in line]
    return "\n".join([*lines, f"INFO: Objects deleted successfully: count={len(PLANNED)}"]) + "\n"


def _backups(*, extra_full: bool = True) -> list[Backup]:
    listed = parse_backups(fixture_text("backup-list.json"))
    if not extra_full:
        return listed
    newest = replace(
        listed[-1],
        name=FULL_4,
        start_time=listed[-1].start_time.replace(day=listed[-1].start_time.day + 7),
    )
    return [*listed, newest]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = make_sandbox(tmp_path, monkeypatch)
    box.put("delete-dry.log", DRY_RUN)
    box.put("delete-confirm.log", _confirm_log())
    return box


# ── reading WAL-G's log ──────────────────────────────────────────────────────


def test_the_real_dry_run_lists_the_expiring_backups_and_wal() -> None:
    assert len(PLANNED) == 21
    assert f"basebackups_005/{FULL_1}_backup_stop_sentinel.json" in PLANNED
    assert f"basebackups_005/{DELTA_OF_1}/metadata.json" in PLANNED
    assert "wal_005/000000010000000000000001.lz4" in PLANNED
    assert all(key.startswith(("basebackups_005/", "wal_005/")) for key in PLANNED)


def test_the_listing_is_read_from_either_stream() -> None:
    on_stdout = parse_marked(WalgOutput(stdout=DRY_RUN, stderr=""), retention._DRY_RUN_COUNT)

    assert on_stdout == PLANNED


def test_a_listing_whose_count_disagrees_with_wal_gs_own_is_refused() -> None:
    tampered = re.sub(r"count=\d+", "count=999", DRY_RUN)

    with pytest.raises(RetentionAbortedError, match="count differs or is missing"):
        parse_marked(WalgOutput(stdout="", stderr=tampered), retention._DRY_RUN_COUNT)


def test_a_listing_without_a_count_line_is_refused() -> None:
    without_count = "\n".join(line for line in DRY_RUN.splitlines() if "Dry run:" not in line)

    with pytest.raises(RetentionAbortedError):
        parse_marked(WalgOutput(stdout="", stderr=without_count), retention._DRY_RUN_COUNT)


def test_an_object_on_another_storage_is_refused() -> None:
    other = "INFO: Object marked for deletion: wal_005/0000000100000000000000FF.lz4 storage=cold\n"

    with pytest.raises(RetentionAbortedError, match="storage 'cold'"):
        parse_marked(WalgOutput(stdout="", stderr=other), retention._DRY_RUN_COUNT)


def test_no_marked_objects_is_an_empty_plan() -> None:
    assert (
        parse_marked(WalgOutput(stdout="", stderr="INFO: Start delete\n"), retention._DRY_RUN_COUNT)
        == []
    )


# ── the three invariants ─────────────────────────────────────────────────────


def test_the_real_dry_run_satisfies_every_invariant() -> None:
    assert_invariants(PLANNED, _backups())


def test_a_key_outside_the_backup_and_wal_prefixes_aborts() -> None:
    other_prefix = "ava-logical/2026-10-01/dump.enc"

    with pytest.raises(RetentionAbortedError, match="outside basebackups_005/ and wal_005/"):
        assert_invariants([*PLANNED, other_prefix], _backups())


def test_a_key_that_merely_resembles_a_prefix_aborts() -> None:
    with pytest.raises(RetentionAbortedError, match="outside"):
        assert_invariants([*PLANNED, "xwal_005/000000010000000000000001.lz4"], _backups())


@pytest.mark.parametrize(
    "key",
    [
        f"basebackups_005/{FULL_4}_backup_stop_sentinel.json",
        f"basebackups_005/{FULL_4}/metadata.json",
        f"basebackups_005/{FULL_4}/tar_partitions/part_001.tar.lz4",
    ],
)
def test_an_object_of_the_newest_backup_aborts(key: str) -> None:
    with pytest.raises(RetentionAbortedError, match="newest backup depends on"):
        assert_invariants([*PLANNED, key], _backups())


def test_an_object_of_a_backup_the_newest_one_is_an_increment_of_aborts() -> None:
    listed = _backups(extra_full=False)
    newest_increment = replace(
        listed[-1],
        name="base_0000000100000006000000C0_D_000000010000000200000020",
        start_time=listed[-1].start_time.replace(day=listed[-1].start_time.day + 7),
    )

    with pytest.raises(RetentionAbortedError, match=re.escape(FULL_2)):
        assert_invariants([f"basebackups_005/{FULL_2}/metadata.json"], [*listed, newest_increment])


def test_an_incomplete_chain_for_the_newest_backup_aborts() -> None:
    listed = [b for b in _backups(extra_full=False) if b.name != FULL_1]
    listed.append(
        replace(
            listed[-1],
            name="base_0000000100000006000000C0_D_000000010000000000000087",
            start_time=listed[-1].start_time.replace(day=listed[-1].start_time.day + 7),
        )
    )

    with pytest.raises(RetentionAbortedError, match="chain is not intact"):
        assert_invariants(PLANNED, listed)


def test_a_plan_that_expires_every_full_backup_aborts_the_survivor_rule() -> None:
    listed = _backups()
    every_sentinel = [f"basebackups_005/{b.name}_backup_stop_sentinel.json" for b in listed]

    with pytest.raises(RetentionAbortedError, match="every full backup"):
        retention.check_a_full_backup_survives(every_sentinel, listed)


def test_the_survivor_rule_passes_while_one_full_backup_keeps_its_sentinel() -> None:
    listed = _backups()
    all_but_newest = [f"basebackups_005/{b.name}_backup_stop_sentinel.json" for b in listed[:-1]]

    retention.check_a_full_backup_survives(all_but_newest, listed)


def test_the_chain_rule_alone_already_refuses_a_plan_that_expires_every_full_backup() -> None:
    listed = _backups()
    every_sentinel = [f"basebackups_005/{b.name}_backup_stop_sentinel.json" for b in listed]

    with pytest.raises(RetentionAbortedError, match="newest backup depends on"):
        assert_invariants(every_sentinel, listed)


# ── the whole procedure ──────────────────────────────────────────────────────


def test_a_safe_plan_is_audited_then_confirmed(sandbox: Sandbox) -> None:
    lines: list[str] = []

    outcome = apply_retention(_backups(), lines.append)

    assert outcome == RetentionOutcome(marked=21, deleted=21)
    assert sandbox.calls() == [
        "delete retain FULL 3 --use-sentinel-time",
        "delete retain FULL 3 --use-sentinel-time --confirm",
    ]
    assert lines[0] == "retention: dry run marks 21 objects for deletion:"
    assert [line.strip() for line in lines[1:22]] == PLANNED, "the whole list is in the report"
    assert lines[-1] == "retention: deleted 21 objects"


def test_a_violated_invariant_never_reaches_confirm(sandbox: Sandbox) -> None:
    bad = DRY_RUN.replace("count=21", "count=22") + (
        f"INFO: Object marked for deletion: basebackups_005/{FULL_4}/metadata.json storage=default\n"
    )
    sandbox.put("delete-dry.log", bad)

    with pytest.raises(RetentionAbortedError, match="newest backup depends on"):
        apply_retention(_backups(), lambda _line: None)

    assert sandbox.calls() == ["delete retain FULL 3 --use-sentinel-time"]


def test_a_confirmed_deletion_that_differs_from_the_audit_is_reported(sandbox: Sandbox) -> None:
    sandbox.put(
        "delete-confirm.log",
        _confirm_log().replace("count=21", "count=22")
        + (
            "INFO: Object marked for deletion: wal_005/0000000100000000000000FF.lz4 storage=default\n"
        ),
    )

    with pytest.raises(RetentionAbortedError, match="not the audited dry run"):
        apply_retention(_backups(), lambda _line: None)


def test_with_no_more_full_backups_than_retained_wal_g_is_not_asked(sandbox: Sandbox) -> None:
    lines: list[str] = []

    outcome = apply_retention(_backups(extra_full=False), lines.append)

    assert outcome == RetentionOutcome(marked=0, deleted=0)
    assert sandbox.calls() == []
    assert lines == ["retention: 3 or fewer full backups, nothing expires yet"]


def test_an_empty_plan_deletes_nothing(sandbox: Sandbox) -> None:
    sandbox.put("delete-dry.log", "INFO: Start delete\nINFO: Evaluating objects for deletion...\n")

    outcome = apply_retention(_backups(), lambda _line: None)

    assert outcome == RetentionOutcome(marked=0, deleted=0)
    assert sandbox.calls() == ["delete retain FULL 3 --use-sentinel-time"]


def test_a_failing_wal_g_delete_is_an_error_and_nothing_is_confirmed(sandbox: Sandbox) -> None:
    sandbox.fail("delete")

    with pytest.raises(WalgCommandError, match="wal-g delete failed"):
        apply_retention(_backups(), lambda _line: None)

    assert sandbox.calls() == ["delete retain FULL 3 --use-sentinel-time"]
