"""Reading `backup-list --detail --json`: ordering, increments and the chain behind one.

`fixtures/backup-list.json` is the list a real WAL-G v3.0.9 printed after three full
backups and one increment of the first (the host name and data directory replaced).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.backup.walg import backups
from services.backup.walg.backups import Backup, BackupChainError, chain_of, parse_backups
from services.backup.walg.tests.support import Sandbox, fixture_text, make_sandbox

FULL_1 = "base_000000010000000000000087"
DELTA_OF_1 = "base_0000000100000000000000A6_D_000000010000000000000087"
FULL_2 = "base_000000010000000200000020"
FULL_3 = "base_0000000100000004000000B6"


def test_the_real_list_parses_oldest_first_with_sizes() -> None:
    listed = parse_backups(fixture_text("backup-list.json"))

    assert [b.name for b in listed] == [FULL_1, DELTA_OF_1, FULL_2, FULL_3]
    assert [b.is_full for b in listed] == [True, False, True, True]
    assert listed[0].uncompressed_bytes == 2452668013
    assert listed[0].compressed_bytes == 288734812
    assert (listed[0].start_lsn, listed[0].finish_lsn) == (2264924200, 2610125464)
    assert listed[1].parent_segment == "000000010000000000000087"
    assert listed[0].parent_segment is None


def test_the_order_is_by_start_time_not_by_the_order_printed() -> None:
    reversed_text = json.dumps(list(reversed(json.loads(fixture_text("backup-list.json")))))

    assert [b.name for b in parse_backups(reversed_text)] == [FULL_1, DELTA_OF_1, FULL_2, FULL_3]


@pytest.mark.parametrize("text", ["", "  \n", "null", "[]"])
def test_no_backups_is_an_empty_list(text: str) -> None:
    assert parse_backups(text) == []


@pytest.mark.parametrize(
    "text",
    ["not json", '{"backup_name": "x"}', '[{"backup_name": "x"}]', '["x"]'],
)
def test_output_that_is_not_a_backup_list_is_an_error(text: str) -> None:
    with pytest.raises(BackupChainError):
        parse_backups(text)


def test_an_increment_depends_on_its_parent_a_full_backup_on_nothing() -> None:
    listed = parse_backups(fixture_text("backup-list.json"))

    assert [b.name for b in chain_of(listed, DELTA_OF_1)] == [DELTA_OF_1, FULL_1]
    assert [b.name for b in chain_of(listed, FULL_3)] == [FULL_3]


def test_a_chain_of_increments_is_followed_to_its_full_backup() -> None:
    listed = parse_backups(fixture_text("backup-list.json"))
    second_delta = Backup(
        name="base_0000000100000000000000C0_D_0000000100000000000000A6",
        start_time=listed[1].start_time.replace(day=listed[1].start_time.day + 1),
        uncompressed_bytes=1,
        compressed_bytes=1,
        start_lsn=1,
        finish_lsn=2,
    )

    chain = chain_of([*listed, second_delta], second_delta.name)

    assert [b.name for b in chain] == [second_delta.name, DELTA_OF_1, FULL_1]


def test_a_missing_parent_is_an_error_not_a_shorter_chain() -> None:
    listed = [b for b in parse_backups(fixture_text("backup-list.json")) if b.name != FULL_1]

    with pytest.raises(BackupChainError, match="matches 0 backups"):
        chain_of(listed, DELTA_OF_1)


def test_an_unknown_backup_is_an_error() -> None:
    with pytest.raises(BackupChainError, match="is not in the backup list"):
        chain_of(parse_backups(fixture_text("backup-list.json")), "base_nope")


def test_listing_asks_wal_g_for_the_detailed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox: Sandbox = make_sandbox(tmp_path, monkeypatch)
    sandbox.put("backups.json", fixture_text("backup-list.json"))

    listed = backups.list_backups()

    assert sandbox.calls() == ["backup-list --detail --json"]
    assert len(listed) == 4
