from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, cast

import pytest
from cryptography.exceptions import InvalidTag

from services.pitr.crypto import create_plan, decrypt_archive, encrypt_archive, open_encrypted
from services.pitr.object_store import ObjectStore, RemoteObjectAck
from services.pitr.uploader import (
    AckCorruptionError,
    AckManifest,
    PitrUploader,
    RemoteCollisionError,
    WalSourceTooLargeError,
)

NAME = "000000010000000000000001"


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, RemoteObjectAck] = {}
        self.puts = 0
        self.deletes = 0
        self.crash_after_put = False
        self.staged_uploads: list[tuple[int, bytes]] = []

    def put_wal_ciphertext_if_absent(
        self, path: Path, object_name: str, metadata: Mapping[str, str]
    ) -> RemoteObjectAck:
        import base64

        import google_crc32c

        self.puts += 1
        self.staged_uploads.append((path.stat().st_ino, path.read_bytes()))
        existing = self.objects.get(object_name)
        if existing is not None:
            # A 412 stat is a read, not a create: the caller must see
            # created=False (the real GCSObjectStore does the same).
            return replace(existing, created=False)
        payload = path.read_bytes()
        ack = RemoteObjectAck(
            object_name,
            1,
            len(payload),
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


def _uploader(tmp_path: Path, store: ObjectStore) -> tuple[PitrUploader, Path]:
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
    """Only the exact durable-plan ciphertext may satisfy a 412."""
    # Crash recovery (port from #405's gated-service test): the remote write
    # landed but the process died before the ACK; the retry meets the 412 and
    # must reuse the same staged bytes and ACK the exact remote object.
    store = FakeStore()
    uploader_crash, source_crash = _uploader(tmp_path / "crash", store)
    store.crash_after_put = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        uploader_crash.upload_one(source_crash)
    store.crash_after_put = False
    uploader_crash.upload_one(source_crash)
    assert store.puts == 2
    assert store.staged_uploads[0] == store.staged_uploads[1]


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("size", 1),
        ("crc32c", "wrong"),
        ("metadata", {"ava-key-id": "wrong"}),
    ],
)
def test_412_size_crc_or_metadata_mismatch_is_critical(
    tmp_path: Path, changed_field: str, changed_value: object
) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    store.crash_after_put = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        uploader.upload_one(source)
    remote = next(iter(store.objects.values()))
    if changed_field == "size":
        changed = replace(remote, size=cast(int, changed_value), created=False)
    elif changed_field == "crc32c":
        changed = replace(remote, crc32c=cast(str, changed_value), created=False)
    elif changed_field == "metadata":
        changed = replace(remote, metadata=cast(dict[str, str], changed_value), created=False)
    else:
        raise AssertionError(changed_field)
    store.objects[remote.object_name] = changed
    store.crash_after_put = False
    with pytest.raises(RemoteCollisionError):
        uploader.upload_one(source)


def test_source_over_64_mib_fails_before_staging(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    with source.open("r+b") as output:
        output.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(WalSourceTooLargeError):
        uploader.upload_one(source)
    assert not list((tmp_path / "staging").iterdir())
    assert store.puts == 0


def test_disk_footprint_counts_spool_and_staging(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    staged = tmp_path / "staging" / "active.enc"
    staged.write_bytes(b"ciphertext")
    footprint = uploader.disk_footprint()
    assert footprint.spool_bytes == source.stat().st_size
    assert footprint.staging_bytes == staged.stat().st_size
    assert footprint.total_bytes == source.stat().st_size + staged.stat().st_size


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


@pytest.mark.parametrize("remaining", [("stage", "plan"), ("plan",), ()])
def test_ack_fast_path_reconciles_every_cleanup_crash_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining: tuple[str, ...],
) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    original_write_ack = uploader._write_ack  # pyright: ignore[reportPrivateUsage]

    def crash_after_ack(ack: AckManifest, destination: Path) -> None:
        original_write_ack(ack, destination)
        raise RuntimeError("crash after durable ACK")

    monkeypatch.setattr(uploader, "_write_ack", crash_after_ack)
    with pytest.raises(RuntimeError, match="crash after durable ACK"):
        uploader.upload_one(source)
    stage = tmp_path / "staging" / f"{NAME}.enc"
    plan = tmp_path / "staging" / f"{NAME}.plan.json"
    if "stage" not in remaining:
        stage.unlink()
    if "plan" not in remaining:
        plan.unlink()
    monkeypatch.setattr(uploader, "_write_ack", original_write_ack)
    uploader.upload_one(source)
    assert not source.exists()
    assert not stage.exists()
    assert not plan.exists()
    assert store.puts == 1


def test_same_size_staged_ciphertext_tamper_fails_before_remote_retry(tmp_path: Path) -> None:
    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    store.crash_after_put = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        uploader.upload_one(source)
    stage = next((tmp_path / "staging").glob("*.enc"))
    damaged = bytearray(stage.read_bytes())
    damaged[len(damaged) // 2] ^= 1
    stage.write_bytes(damaged)
    store.crash_after_put = False
    with pytest.raises(AckCorruptionError, match="differs from its plan"):
        uploader.upload_one(source)
    assert store.puts == 1


@pytest.mark.asyncio
async def test_critical_backoff_heartbeats_and_stops_promptly() -> None:
    from services.pitr.uploader_daemon import _wait_with_heartbeat
    from shared.daemon_health import Liveness

    stop = asyncio.Event()
    liveness = Liveness(timeout_s=0.04)
    waiter = asyncio.create_task(_wait_with_heartbeat(stop, 1.0, liveness, heartbeat_interval=0.01))
    await asyncio.sleep(0.12)
    assert liveness.is_alive()
    stop.set()
    await asyncio.wait_for(waiter, timeout=0.1)


@pytest.mark.parametrize(
    ("extra_staging_bytes", "expected_status"),
    [(0, "ok"), (8, "degraded"), (24, "down")],
)
def test_disk_health_counts_spool_stage_and_temp_files(
    tmp_path: Path, extra_staging_bytes: int, expected_status: str
) -> None:
    from services.pitr.uploader_daemon import _disk_components

    store = FakeStore()
    uploader, source = _uploader(tmp_path, store)
    source.write_bytes(b"s" * 8)
    if extra_staging_bytes:
        (tmp_path / "staging" / ".ciphertext.tmp").write_bytes(b"x" * extra_staging_bytes)
    component = _disk_components(uploader, warn_bytes=16, hard_bytes=32)[0]
    assert component["status"] == expected_status


# ── QA #920 block 1: real >8 MiB payloads must upload (resumable seek/tell) ─


class _ResumableFakeBlob:
    """A storage Blob whose upload mimics the SDK's resumable session: it
    opens the source and calls tell()/seek() exactly like the transport does
    past ~8 MiB. A non-seekable source (an in-memory encrypted stream) raises
    UnsupportedOperation here — the exact QA #920 block-1 failure."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.metadata: dict[str, str] = {}
        self.generation: int | None = None
        self.size: int | None = None
        self.crc32c: str | None = None
        self.data = b""

    def upload_from_filename(self, filename: str, **kwargs: object) -> None:
        import base64

        import google_crc32c

        with open(filename, "rb") as source:  # noqa: PTH123 — the SDK opens by path
            source.seek(0, os.SEEK_END)
            total = source.tell()
            source.seek(0)
            self.data = source.read()
        self.size = total
        self.crc32c = base64.b64encode(google_crc32c.Checksum(self.data).digest()).decode()
        self.generation = 1

    def reload(self, **kwargs) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        return None


class _ResumableFakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _ResumableFakeBlob] = {}

    def blob(self, name: str) -> _ResumableFakeBlob:
        blob = _ResumableFakeBlob(name)
        self.blobs[name] = blob
        return blob


class _ResumableFakeClient:
    def __init__(self) -> None:
        self._bucket = _ResumableFakeBucket()

    def bucket(self, _name: str) -> _ResumableFakeBucket:
        return self._bucket


def test_filename_adapter_uses_seekable_16mib_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA #920 block 1 regression: the adapter receives a seekable WAL file.

    The path under test is the production one end to end: the real streaming
    encryption (open_encrypted) writes the staging file, and GCSObjectStore
    uploads it through a boundary fake whose upload_from_filename
    performs the resumable tell()/seek() dance. A regression back to
    streamed upload fails here with UnsupportedOperation (the SDK's resumable
    transport cannot tell() a BufferedReader) even though FakeStore-based
    tests stay green."""
    from services.pitr import gcs_store as gcs_store_module
    from services.pitr.gcs_store import GCSObjectStore
    from services.pitr.uploader import PitrUploader

    client = _ResumableFakeClient()
    monkeypatch.setattr(
        gcs_store_module.storage,
        "Client",
        lambda *_a, **_k: client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        gcs_store_module.service_account.Credentials,
        "from_service_account_file",
        lambda *_a, **_k: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    creds.chmod(0o600)
    real_store = GCSObjectStore(
        project="proj", bucket="bucket", credentials_file=creds, timeout_seconds=30
    )

    spool = tmp_path / "big-spool"
    ack_dir = tmp_path / "big-ack"
    staging = tmp_path / "big-staging"
    for directory in (spool, ack_dir, staging):
        directory.mkdir(parents=True)
    source = spool / NAME
    source.write_bytes(b"w" * (16 * 1024 * 1024))  # 16 MiB — past the 8 MiB resumable floor
    uploader = PitrUploader(
        spool=spool,
        ack_dir=ack_dir,
        staging=staging,
        prefix="prod",
        key=b"k" * 32,
        key_id="v1",
        store=real_store,
    )
    ack = uploader.upload_one(source)
    blob = client._bucket.blobs[ack.object_name]
    assert ack.ciphertext_size == blob.size == len(blob.data)
    assert ack.ciphertext_crc32c == blob.crc32c
    assert not source.exists()
    assert not list(staging.iterdir()), "staging (plan + ciphertext) cleaned after ACK"
    assert blob.data.startswith(b"AVAPITR1"), "the stored object is the encrypted archive"


def test_real_sdk_resumable_transport_retries_16mib_upload(tmp_path: Path) -> None:
    """Run the installed SDK's resumable session against a local HTTP transport."""

    import base64
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    import google_crc32c
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    from services.pitr.gcs_store import BucketClient, GCSObjectStore

    class ResumableHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        puts = 0
        uploaded = b""
        object_metadata: ClassVar[dict[str, str]] = {}
        object_name = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        @classmethod
        def _object(cls) -> dict[str, object]:
            crc = base64.b64encode(google_crc32c.Checksum(cls.uploaded).digest()).decode()
            return {
                "name": cls.object_name,
                "generation": "1",
                "size": str(len(cls.uploaded)),
                "crc32c": crc,
                "metadata": cls.object_metadata,
            }

        def do_POST(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            assert query["uploadType"] == ["resumable"]
            assert query["ifGenerationMatch"] == ["0"]
            length = int(self.headers["Content-Length"])
            request = cast(dict[str, object], json.loads(self.rfile.read(length)))
            type(self).object_name = cast(str, request["name"])
            type(self).object_metadata = cast(dict[str, str], request["metadata"])
            self.send_response(200)
            self.send_header("Location", f"http://127.0.0.1:{server.server_port}/upload-session")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self) -> None:
            length = int(self.headers["Content-Length"])
            payload = self.rfile.read(length)
            type(self).puts += 1
            if type(self).puts == 1:
                self._json(503, {"error": {"message": "retry this chunk"}})
                return
            type(self).uploaded = payload
            self._json(200, type(self)._object())

        def do_GET(self) -> None:
            self._json(200, type(self)._object())

    server = ThreadingHTTPServer(("127.0.0.1", 0), ResumableHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        client = storage.Client(
            project="proj",
            credentials=AnonymousCredentials(),
            client_options=ClientOptions(api_endpoint=endpoint),
        )
        store = GCSObjectStore.from_bucket_client(
            cast(BucketClient, client.bucket("bucket")), timeout_seconds=5
        )
        uploader, source = _uploader(tmp_path, store)
        source.write_bytes(b"w" * (16 * 1024 * 1024))
        ack = uploader.upload_one(source)
        assert ResumableHandler.puts == 2
        assert len(ResumableHandler.uploaded) == ack.ciphertext_size
        assert ack.ciphertext_crc32c == ResumableHandler._object()["crc32c"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
