"""Rollback-snapshot archive workflow contracts without a live cluster."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from services.pitr.checksums import CRC32C, ObjectChecksum, digest_bytes
from services.pitr.object_store import RemoteObjectAck
from services.pitr.restore_manifest import RestoreObject
from services.pitr.rollback_snapshot_archive import (
    SnapshotArchiveNotVerifiedError,
    archive_rollback_snapshot,
    retire_rollback_snapshot,
    verify_rollback_snapshot,
)

_FAKE_PIN_TOKEN = str(42)
_REOBSERVED_PIN_TOKEN = str(43)


class _Store:
    def __init__(self, *, created: bool = True, pin_token: str = _FAKE_PIN_TOKEN) -> None:
        self.objects: dict[str, bytes] = {}
        self.acks: dict[str, RemoteObjectAck] = {}
        self.puts: list[str] = []
        self._created = created
        self._pin_token = pin_token

    def put_wal_ciphertext_if_absent(
        self, path: Path, object_name: str, metadata: Mapping[str, str]
    ) -> RemoteObjectAck:
        self.puts.append(object_name)
        payload = path.read_bytes()
        if object_name not in self.objects:
            self.objects[object_name] = payload
            self.acks[object_name] = RemoteObjectAck(
                object_name=object_name,
                pin_token=self._pin_token,
                size=len(payload),
                checksum=ObjectChecksum(CRC32C, digest_bytes(CRC32C, payload)),
                metadata=dict(metadata),
                created=self._created,
            )
        return self.acks[object_name]

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        return self.acks.get(object_name)


class _Reader:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.expected: RestoreObject | None = None

    def download_exact(self, expected: RestoreObject, destination: Path) -> None:
        self.expected = expected
        destination.write_bytes(self._store.objects[expected.object_name])


def _export(table: str, destination: Path) -> None:
    assert table == "agent_state_backfill_snapshot"
    destination.write_bytes(b"custom-format-table-dump")


def test_archive_publishes_aes_gcm_ciphertext_and_persists_pinned_evidence(tmp_path: Path) -> None:
    store = _Store()

    record = archive_rollback_snapshot(
        "agent_state_backfill_snapshot",
        ava_home=tmp_path / "home",
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=_export,
        store=store,
    )

    assert record.table == "agent_state_backfill_snapshot"
    assert record.verified_at is None
    assert record.object_name in store.objects
    assert record.pin_token == _FAKE_PIN_TOKEN
    assert (tmp_path / "home" / "rollback-snapshot-archives" / f"{record.table}.json").exists()
    assert store.acks[record.object_name].metadata == {
        "ava-artifact-kind": "rollback-snapshot",
        "ava-key-id": "archive-key-v1",
        "ava-rollback-snapshot-table": record.table,
    }


def test_archive_short_circuits_after_persisting_evidence_without_another_export_or_put(
    tmp_path: Path,
) -> None:
    store = _Store()
    export_count = 0

    def export_once(table: str, destination: Path) -> None:
        nonlocal export_count
        export_count += 1
        _export(table, destination)

    archived = archive_rollback_snapshot(
        "agent_state_backfill_snapshot",
        ava_home=tmp_path / "home",
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=export_once,
        store=store,
    )

    resumed = archive_rollback_snapshot(
        archived.table,
        ava_home=tmp_path / "home",
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=export_once,
        store=store,
    )

    assert resumed == archived
    assert export_count == 1
    assert store.puts == [archived.object_name]


def test_archive_records_the_reobserved_identity_after_a_crash_before_evidence_persists(
    tmp_path: Path,
) -> None:
    store = _Store(created=False, pin_token=_REOBSERVED_PIN_TOKEN)

    record = archive_rollback_snapshot(
        "agent_state_backfill_snapshot",
        ava_home=tmp_path / "home",
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=_export,
        store=store,
    )

    ack = store.acks[record.object_name]
    assert ack.created is False
    assert record.pin_token == _REOBSERVED_PIN_TOKEN
    assert record.size == ack.size
    assert record.metadata == tuple(sorted(ack.metadata.items()))


def test_verify_downloads_the_exact_generation_decrypts_it_and_marks_the_evidence(
    tmp_path: Path,
) -> None:
    store = _Store()
    home = tmp_path / "home"
    archived = archive_rollback_snapshot(
        "agent_state_backfill_snapshot",
        ava_home=home,
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=_export,
        store=store,
    )
    reader = _Reader(store)
    drills: list[tuple[str, bytes]] = []

    verified = verify_rollback_snapshot(
        archived.table,
        ava_home=home,
        key=b"k" * 32,
        reader=reader,
        restore_drill=lambda table, dump: drills.append((table, dump.read_bytes())),
    )

    assert reader.expected is not None
    assert reader.expected.object_name == archived.object_name
    assert reader.expected.pin_token == archived.pin_token
    assert drills == [(archived.table, b"custom-format-table-dump")]
    assert verified.verified_at is not None


def test_retire_refuses_until_the_exact_archived_artifact_has_passed_a_restore_drill(
    tmp_path: Path,
) -> None:
    store = _Store()
    home = tmp_path / "home"
    archived = archive_rollback_snapshot(
        "agent_state_backfill_snapshot",
        ava_home=home,
        key=b"k" * 32,
        key_id="archive-key-v1",
        export_table=_export,
        store=store,
    )
    retired: list[str] = []

    with pytest.raises(SnapshotArchiveNotVerifiedError):
        retire_rollback_snapshot(archived.table, ava_home=home, drop_table=retired.append)

    verify_rollback_snapshot(
        archived.table,
        ava_home=home,
        key=b"k" * 32,
        reader=_Reader(store),
        restore_drill=lambda _table, _dump: None,
    )

    retire_rollback_snapshot(archived.table, ava_home=home, drop_table=retired.append)
    assert retired == [archived.table]


def test_archive_accepts_only_the_snapshot_name_convention_shared_with_migration_lint(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=r"\*_backfill_\*"):
        archive_rollback_snapshot(
            "durable_application_table",
            ava_home=tmp_path / "home",
            key=b"k" * 32,
            key_id="archive-key-v1",
            export_table=_export,
            store=_Store(),
        )
