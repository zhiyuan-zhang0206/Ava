from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest
from cryptography.exceptions import InvalidTag

from services.pitr.crypto import create_plan, decrypt_archive, encrypt_archive, open_encrypted
from services.pitr.object_store import RemoteObjectAck
from services.pitr.uploader import AckCorruptionError, PitrUploader, RemoteCollisionError

NAME = "000000010000000000000001"


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, RemoteObjectAck] = {}
        self.puts = 0
        self.deletes = 0
        self.crash_after_put = False

    def put_stream_if_absent(
        self,
        open_source: Callable[[], BinaryIO],
        size: int,
        object_name: str,
        metadata: Mapping[str, str],
    ) -> RemoteObjectAck:
        import base64

        import google_crc32c

        self.puts += 1
        existing = self.objects.get(object_name)
        if existing is not None:
            return existing
        with open_source() as source:
            payload = source.read()
        ack = RemoteObjectAck(
            object_name,
            1,
            size,
            base64.b64encode(google_crc32c.Checksum(payload).digest()).decode(),
            dict(metadata),
            True,
        )
        self.objects[object_name] = ack
        if self.crash_after_put:
            raise RuntimeError("simulated crash after remote verification")
        return ack

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        return self.objects.get(object_name)


def _uploader(tmp_path: Path, store: FakeStore) -> tuple[PitrUploader, Path]:
    spool = tmp_path / "spool"
    ack = tmp_path / "ack"
    staging = tmp_path / "staging"
    for directory in (spool, ack, staging):
        directory.mkdir(parents=True)
    source = spool / NAME
    source.write_bytes(b"wal" * 4096)
    return PitrUploader(
        spool=spool,
        ack_dir=ack,
        staging=staging,
        prefix="prod",
        key=b"k" * 32,
        key_id="v1",
        store=store,
    ), source


def test_stream_roundtrip_and_tamper(tmp_path: Path) -> None:
    source = tmp_path / NAME
    source.write_bytes(b"wal" * 4096)
    encrypted = tmp_path / "wal.enc"
    restored = tmp_path / "restored"
    encrypt_archive(source, encrypted, key=b"k" * 32, key_id="v1", object_name="p/wal")
    decrypt_archive(encrypted, restored, key=b"k" * 32)
    assert restored.read_bytes() == source.read_bytes()
    damaged = bytearray(encrypted.read_bytes())
    damaged[-17] ^= 1
    encrypted.unlink()
    encrypted.write_bytes(damaged)
    with pytest.raises(InvalidTag):
        decrypt_archive(encrypted, tmp_path / "damaged", key=b"k" * 32)


def test_encryption_plan_reopens_identically_and_survives_restart(tmp_path: Path) -> None:
    source = tmp_path / NAME
    source.write_bytes(b"wal" * 4096)
    plan = create_plan(source, key_id="v1", object_name="prod/wal/object")
    first = open_encrypted(source, key=b"k" * 32, plan=plan).read()
    serialized = __import__("json").loads(__import__("json").dumps(plan.__dict__))
    restarted = type(plan)(**serialized)
    second = open_encrypted(source, key=b"k" * 32, plan=restarted).read()
    assert first == second
    assert len(first) == plan.ciphertext_size


def test_verified_upload_writes_ack_then_removes_local_files(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    ack = uploader.upload_one(source)
    assert ack.object_name == f"prod/wal/{NAME[:8]}/{NAME}.enc"
    assert not source.exists()
    assert (tmp_path / "ack" / f"{NAME}.ack.json").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "staging").iterdir())
    assert store.deletes == 0


def test_412_exact_remote_is_idempotent_but_collision_is_critical(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    store.crash_after_put = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        uploader.upload_one(source)
    store.crash_after_put = False
    uploader.upload_one(source)
    assert store.puts == 2

    store.objects[next(iter(store.objects))] = replace(
        next(iter(store.objects.values())), metadata={"ava-key-id": "wrong"}, created=False
    )
    uploader3, source3 = _uploader(tmp_path / "collision", store)
    with pytest.raises(RemoteCollisionError):
        uploader3.upload_one(source3)


def test_durable_ack_recovers_cleanup_without_remote_call(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    uploader.upload_one(source)
    source.write_bytes(b"wal" * 4096)
    uploader.upload_one(source)
    assert store.puts == 1
    assert not source.exists()


def test_corrupt_ack_never_deletes_spool(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    (tmp_path / "ack" / f"{NAME}.ack.json").write_text("not json")
    with pytest.raises(AckCorruptionError):
        uploader.upload_one(source)
    assert source.exists()


def test_corrupt_plan_fails_closed_before_remote_write(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    (tmp_path / "staging" / f"{NAME}.plan.json").write_text("not json")
    with pytest.raises(AckCorruptionError):
        uploader.upload_one(source)
    assert source.exists()
    assert store.puts == 0
