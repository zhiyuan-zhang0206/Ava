"""Baidu PCS client and upload-engine tests against an in-memory control
plane (see ``baidu_test_support`` for the fake)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

import services.pitr.baidu_store as baidu_store_module
from services.pitr.baidu_pcs import (
    _PERMANENT_ERRNOS,
    _TRANSIENT_ERRNOS,
    PcsClient,
    PcsError,
    PcsPermanentError,
    PcsTransientError,
    RemoteFile,
    _check_errno,
)
from services.pitr.baidu_store import SVIP_SINGLE_FILE_LIMIT_BYTES
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import (
    PermanentObjectStoreError,
    RemoteObjectAck,
    TransientObjectStoreError,
)
from tests.services.baidu_test_support import (
    APP_ROOT,
    OBJECT,
    ChunkSource,
    FakePcs,
    _md5,
    _opaque,
    make_store,
    sidecar_json,
)

# ── PCS client: errno + HTTP mapping ──


@pytest.mark.parametrize("errno", sorted(_TRANSIENT_ERRNOS))
def test_transient_errnos_raise_transient(errno: int) -> None:
    with pytest.raises(PcsTransientError):
        _check_errno({"errno": errno, "errmsg": "boom"})


@pytest.mark.parametrize("errno", sorted(_PERMANENT_ERRNOS))
def test_permanent_errnos_raise_permanent(errno: int) -> None:
    with pytest.raises(PcsPermanentError):
        _check_errno({"errno": errno, "errmsg": "boom"})


def test_unreviewed_quota_errno_is_transient() -> None:
    with pytest.raises(PcsTransientError):
        _check_errno({"errno": 20012, "errmsg": "quota"})


def test_unknown_errno_raises_plain_and_zero_passes() -> None:
    with pytest.raises(PcsError):
        _check_errno({"errno": 424242, "errmsg": "boom"})
    _check_errno({"errno": 0})
    _check_errno({})


def test_payload_maps_http_failures() -> None:
    with pytest.raises(PcsTransientError):
        PcsClient._payload(httpx.Response(500), "https://pan.baidu.com/x")
    with pytest.raises(PcsPermanentError):
        PcsClient._payload(httpx.Response(403), "https://pan.baidu.com/x")
    with pytest.raises(PcsTransientError):
        PcsClient._payload(httpx.Response(200, content=b"not json"), "https://pan.baidu.com/x")
    with pytest.raises(PcsTransientError):
        PcsClient._payload(httpx.Response(200, json=[1]), "https://pan.baidu.com/x")
    assert PcsClient._payload(httpx.Response(200, json={"errno": 0}), "u") == {"errno": 0}


# ── PCS client: endpoint shapes ──


def test_precreate_parses_and_forwards_resume_uploadid() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"uploadid": "u7", "return_type": 1, "block_list": ["aa", "bb"]},
        )

    client = PcsClient("t", transport=httpx.MockTransport(handler))
    result = client.precreate(
        path="/apps/p", size=10, block_list=["aa", "bb"], rtype=3, uploadid="u7"
    )
    assert result.uploadid == "u7"
    assert result.return_type == 1
    assert result.missing_blocks == ("aa", "bb")
    assert captured["uploadid"] == "u7"
    assert json.loads(captured["block_list"]) == ["aa", "bb"]


def test_create_parses_the_read_back_row() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # P0 smoke: create takes its business params in the form body —
        # the query string carries only the method, and rtype is absent
        # (a precreate-only policy).
        assert dict(request.url.params) == {"method": "create"}
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "fs_id": 5,
                "path": "/apps/p",
                "size": 10,
                "md5": "m",
                "isdir": 0,
            },
        )

    row = PcsClient("t", transport=httpx.MockTransport(handler)).create(
        path="/apps/p", size=10, block_list=["m"], uploadid="u"
    )
    assert row == RemoteFile(fs_id=5, path="/apps/p", size=10, md5="m", isdir=0)
    assert captured["path"] == "/apps/p"
    assert captured["uploadid"] == "u"
    assert json.loads(captured["block_list"]) == ["m"]
    assert "rtype" not in captured


def test_filemetas_returns_none_when_absent_and_passes_dlink() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"list": []})

    client = PcsClient("t", transport=httpx.MockTransport(handler))
    assert client.filemetas(7, dlink=True) is None
    assert captured["dlink"] == "1"


def test_list_dir_parses_rows_and_delete_sends_filelist() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if dict(request.url.params).get("method") == "list":
            return httpx.Response(
                200,
                json={"list": [{"fs_id": 1, "path": "/p", "size": 2, "md5": "m", "isdir": 0}]},
            )
        return httpx.Response(200, json={"errno": 0})

    client = PcsClient("t", transport=httpx.MockTransport(handler))
    rows = client.list_dir("/p", recursion=1)
    assert rows[0].fs_id == 1
    client.delete_files(["/p/a", "/p/b"])
    sent = json.loads(urllib.parse.parse_qs(bodies[-1].decode())["filelist"][0])
    assert sent == ["/p/a", "/p/b"]


# ── store engine: three-phase upload ──


def test_put_wal_ciphertext_three_phase_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    payload = b"wal-ciphertext" * 256  # one 32 MiB shard
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)
    metadata = {"ava-archive-name": "000000010000000000000001"}
    ack = store.put_wal_ciphertext_if_absent(source, OBJECT, metadata)

    whole_md5 = _md5(payload)
    assert ack.size == len(payload)
    assert ack.checksum == ObjectChecksum(MD5, whole_md5)
    assert ack.created is True
    obj_path = f"{APP_ROOT}/{OBJECT}"
    assert ack.pin_token == f"{fake.files[obj_path]['fs_id']}:{_opaque(payload)}"
    assert fake.calls[:3] == [
        f"precreate {obj_path}",
        f"upload {obj_path}",
        f"create {obj_path}",
    ]
    # The sidecar mirrors the ACK identity through the same engine.
    sidecar = fake.files[f"{obj_path}.ack.json"]
    expected = sidecar_json(OBJECT, ack)
    assert sidecar["md5"] == _opaque(expected)
    assert sidecar["size"] == len(expected)


def test_resume_uploads_only_missing_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(baidu_store_module, "SVIP_SHARD_BYTES", 5)
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    payload = b"0123456789AB"  # shards of 5, 5, 2 bytes
    obj_path = f"{APP_ROOT}/ava-pitr/wal/x"
    fake.parts[obj_path] = {0: payload[:5], 2: payload[10:]}
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(source, "ava-pitr/wal/x", {})

    assert ack.created is True
    assert ack.checksum == ObjectChecksum(MD5, _md5(payload))
    obj_upload_calls = [call for call in fake.calls if call == f"upload {obj_path}"]
    assert len(obj_upload_calls) == 1
    assert fake.parts[obj_path][1] == payload[5:10]


def test_put_base_streams_two_identical_walks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(baidu_store_module, "SVIP_SHARD_BYTES", 7)
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    chunks = [b"0123456789", b"ABCDEFGHIJ"]
    payload = b"".join(chunks)
    source = ChunkSource(chunks)

    ack = store.put_base_if_absent(source=source, object_name="ava-pitr/base/x", metadata={})

    assert ack.created is True
    assert ack.checksum == ObjectChecksum(MD5, _md5(payload))
    assert source.walks == 2
    obj_path = f"{APP_ROOT}/ava-pitr/base/x"
    assert b"".join(fake.parts[obj_path].get(i, b"") for i in range(3)) == payload


def test_rapid_transfer_adopts_existing_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    payload = b"rapid"
    whole_md5 = _md5(payload)
    obj_path = f"{APP_ROOT}/{OBJECT}"
    fake.seed_file(obj_path, size=len(payload), md5=whole_md5)
    fake.rapid_paths.add(obj_path)
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(source, OBJECT, {})

    assert ack.created is False
    assert ack.checksum == ObjectChecksum(MD5, whole_md5)
    assert f"upload {obj_path}" not in fake.calls


def test_content_collision_is_permanent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    fake.collision_paths.add(f"{APP_ROOT}/{OBJECT}")
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")

    with pytest.raises(PermanentObjectStoreError, match="precreate"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


def test_precreate_transient_errno_maps_to_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    fake.transient_paths.add(f"{APP_ROOT}/{OBJECT}")
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")

    with pytest.raises(TransientObjectStoreError, match="precreate"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


def test_precreate_numeric_index_missing_blocks_upload_by_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 smoke: the live precreate reports missing shards as part indexes
    ("0"), not md5s — the engine uploads those indexes directly."""
    monkeypatch.setattr(baidu_store_module, "SVIP_SHARD_BYTES", 5)
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    payload = b"0123456789AB"
    obj_path = f"{APP_ROOT}/ava-pitr/wal/x"
    fake.parts[obj_path] = {0: payload[:5], 2: payload[10:]}
    fake.missing_override[obj_path] = ["1"]
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(source, "ava-pitr/wal/x", {})

    assert ack.created is True
    assert fake.parts[obj_path][1] == payload[5:10]


def test_precreate_out_of_range_index_is_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    fake.missing_override[f"{APP_ROOT}/{OBJECT}"] = ["7"]
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")

    with pytest.raises(PermanentObjectStoreError, match="out-of-range"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


def test_precreate_unknown_shard_digest_is_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    fake.missing_override[f"{APP_ROOT}/{OBJECT}"] = ["deadbeef"]
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")

    with pytest.raises(PermanentObjectStoreError, match="unknown shard"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


# ── store engine: size guards and cancellation ──


def test_over_single_file_limit_fails_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    source = ChunkSource([b"x"], size=SVIP_SINGLE_FILE_LIMIT_BYTES + 1)

    with pytest.raises(PermanentObjectStoreError, match="single-file limit"):
        store.put_base_if_absent(source=source, object_name="ava-pitr/base/x", metadata={})

    assert source.walks == 0
    assert fake.calls == []


def test_over_shard_ceiling_fails_before_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baidu_store_module, "SVIP_SHARD_BYTES", 1)
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    source = ChunkSource([b"x"], size=1025)

    with pytest.raises(PermanentObjectStoreError, match="shard ceiling"):
        store.put_base_if_absent(source=source, object_name="ava-pitr/base/x", metadata={})

    assert source.walks == 0
    assert fake.calls == []


def test_cancelled_base_upload_raises_before_walking(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    source = ChunkSource([b"x"])

    with pytest.raises(RuntimeError, match="cancelled"):
        store.put_base_if_absent(
            source=source,
            object_name="ava-pitr/base/x",
            metadata={},
            cancelled=lambda: True,
        )

    assert source.walks == 0
    assert fake.calls == []


# ── sidecar discipline ──


def test_existing_sidecar_mismatch_is_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    other = sidecar_json(
        OBJECT,
        RemoteObjectAck(
            object_name=OBJECT,
            pin_token="9:z",  # noqa: S106 — test fixture identity
            size=1,
            checksum=ObjectChecksum(MD5, "z"),
            metadata={},
            created=False,
        ),
    )
    fake.seed_file(
        f"{APP_ROOT}/{OBJECT}.ack.json",
        size=len(other),
        md5=_md5(other),
        dlink="https://dl.test/side",
    )

    def fake_get(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=other)

    monkeypatch.setattr(httpx, "get", fake_get)
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")

    with pytest.raises(PermanentObjectStoreError, match="sidecar differs"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


def test_retry_adopts_a_new_pin_when_only_the_pin_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live P0 smoke: create-on-existing replaces the file object with a
    new fs_id, so a crash retry re-derives a pin that differs only in the
    pin_token — the retry adopts it while the content identity matches."""
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    obj_path = f"{APP_ROOT}/{OBJECT}"
    payload = b"payload"
    digest = _md5(payload)
    opaque = _opaque(payload)
    old_row = fake.seed_file(obj_path, size=len(payload), md5=opaque)
    old_sidecar = sidecar_json(
        OBJECT,
        RemoteObjectAck(
            object_name=OBJECT,
            pin_token=f"{old_row['fs_id']}:{opaque}",
            size=len(payload),
            checksum=ObjectChecksum(MD5, digest),
            metadata={},
            created=False,
        ),
    )
    fake.seed_file(
        f"{obj_path}.ack.json",
        size=len(old_sidecar),
        md5=_md5(old_sidecar),
        dlink="https://dl.test/side",
    )

    def fake_get(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=old_sidecar)

    monkeypatch.setattr(httpx, "get", fake_get)
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(source, OBJECT, {})

    # the fake's create assigned a fresh fs_id — the retry adopted it
    assert ack.pin_token != f"{old_row['fs_id']}:{opaque}"
    assert ack.checksum == ObjectChecksum(MD5, digest)
    rewritten = sidecar_json(OBJECT, ack)
    assert fake.files[f"{obj_path}.ack.json"]["md5"] == _opaque(rewritten)


def test_stat_reads_back_sidecar_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    obj_path = f"{APP_ROOT}/{OBJECT}"
    digest = _md5(b"x")
    row = fake.seed_file(obj_path, size=100, md5=digest)
    sidecar = {
        "object_name": OBJECT,
        "pin_token": f"{row['fs_id']}:{digest}",
        "size": 100,
        "checksum_algo": "md5",
        "checksum_value": digest,
        "metadata": {"ava-archive-name": "000000010000000000000001"},
    }
    sidecar_data = json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(
        f"{obj_path}.ack.json",
        size=len(sidecar_data),
        md5=_md5(sidecar_data),
        dlink="https://dl.test/side",
    )

    def fake_get(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=sidecar_data)

    monkeypatch.setattr(httpx, "get", fake_get)

    ack = store.stat(OBJECT)

    assert ack is not None
    assert ack.pin_token == f"{row['fs_id']}:{digest}"
    assert ack.size == 100
    assert ack.checksum == ObjectChecksum(MD5, digest)
    assert ack.created is False


def test_upload_rejects_a_tampered_read_back_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA #1147 C3: the create read-back size is the engine's cross-check
    — a tampered size must fail the upload, not ACK it."""
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")
    fake.create_override[f"{APP_ROOT}/{OBJECT}"] = {
        "fs_id": 500,
        "path": f"{APP_ROOT}/{OBJECT}",
        "size": 999,
        "md5": _md5(b"payload"),
        "isdir": 0,
    }

    with pytest.raises(PermanentObjectStoreError, match="differs from the local archive"):
        store.put_wal_ciphertext_if_absent(source, OBJECT, {})


def test_upload_accepts_an_opaque_server_row_md5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live P0 smoke: the PCS row md5 is Baidu's encrypted server digest
    (non-hex, never the content md5) — it identifies the row, and the
    upload must not compare it against the content md5."""
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    source = tmp_path / "wal.enc"
    source.write_bytes(b"payload")
    fake.create_override[f"{APP_ROOT}/{OBJECT}"] = {
        "fs_id": 500,
        "path": f"{APP_ROOT}/{OBJECT}",
        "size": len(b"payload"),
        "md5": "1bfd89f2frf619ce44912f39c96003d9",
        "isdir": 0,
    }

    ack = store.put_wal_ciphertext_if_absent(source, OBJECT, {})

    assert ack.pin_token == "500:1bfd89f2frf619ce44912f39c96003d9"  # noqa: S105
    assert ack.checksum == ObjectChecksum(MD5, _md5(b"payload"))


def test_stat_rejects_row_that_differs_from_its_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA #1147 C3: stat must cross-check the file row against the sidecar
    identity — a drifted row is a permanent error, never a silent None."""
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    obj_path = f"{APP_ROOT}/{OBJECT}"
    digest = _md5(b"x")
    row = fake.seed_file(obj_path, size=101, md5=digest)
    sidecar = {
        "object_name": OBJECT,
        "pin_token": f"{row['fs_id']}:{digest}",
        "size": 100,
        "checksum_algo": "md5",
        "checksum_value": digest,
        "metadata": {},
    }
    sidecar_data = json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(
        f"{obj_path}.ack.json",
        size=len(sidecar_data),
        md5=_md5(sidecar_data),
        dlink="https://dl.test/side",
    )

    def fake_get(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=sidecar_data)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(PermanentObjectStoreError, match="differs from its sidecar"):
        store.stat(OBJECT)


def test_file_lookup_pages_beyond_the_first_listing_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent-directory lookup pages with start/limit; an object beyond
    the first 1000 rows must still resolve."""
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    directory = f"{APP_ROOT}/ava-pitr/wal/00000001"
    for index in range(1005):
        fake.seed_file(f"{directory}/filler-{index:04d}.enc", size=1, md5="m")
    obj_path = f"{directory}/000000010000000000000001.enc"
    digest = _md5(b"x")
    row = fake.seed_file(obj_path, size=100, md5=digest)
    sidecar = {
        "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
        "pin_token": f"{row['fs_id']}:{digest}",
        "size": 100,
        "checksum_algo": "md5",
        "checksum_value": digest,
        "metadata": {},
    }
    sidecar_data = json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(
        f"{obj_path}.ack.json",
        size=len(sidecar_data),
        md5=_md5(sidecar_data),
        dlink="https://dl.test/side",
    )

    def fake_get(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=sidecar_data)

    monkeypatch.setattr(httpx, "get", fake_get)

    ack = store.stat("ava-pitr/wal/00000001/000000010000000000000001.enc")

    assert ack is not None
    assert ack.pin_token == f"{row['fs_id']}:{digest}"


def test_stat_returns_none_without_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    fake.seed_file(f"{APP_ROOT}/{OBJECT}", size=100, md5=_md5(b"x"))

    assert store.stat(OBJECT) is None


def test_opaque_pin_flows_from_upload_into_wal_evidence_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA #1201 P0/P2-1: the live-shaped opaque pin must survive the whole
    chain — the store upload produces it and the WAL remote evidence
    validator accepts it without any cross-check against the content md5."""
    from services.pitr.activation_evidence import validate_wal_remote_evidence

    fake = FakePcs()
    store = make_store(fake, monkeypatch)
    payload = b"wal-ciphertext" * 256
    source = tmp_path / "wal.enc"
    source.write_bytes(payload)

    ack = store.put_wal_ciphertext_if_absent(
        source, OBJECT, {"ava-archive-name": "000000010000000000000001"}
    )

    assert ack.pin_token == f"{fake.files[f'{APP_ROOT}/{OBJECT}']['fs_id']}:{_opaque(payload)}"
    segment = "000000010000000000000001"
    evidence = {
        "timeline": "1",
        "segment": segment,
        "bucket_name": "/apps/ava-pitr",
        "object_prefix": "ava-pitr",
        "object_name": ack.object_name,
        "generation": ack.pin_token,
        "ciphertext_size": str(ack.size),
        "ciphertext_crc32c": ack.checksum.value,
        "source_sha256": "1" * 64,
        "source_size": str(len(payload)),
        "key_id": "key",
        "encryption_format": "AVAPITR1",
        "acknowledged_at": "2026-08-31T03:30:00+00:00",
    }
    validate_wal_remote_evidence(
        ack=evidence,
        viewer={**evidence, "viewer_id": "app-key", "observed_at": "2026-08-31T03:31:00+00:00"},
        exact={
            "timeline": "1",
            "segment": segment,
            "switch_lsn": "0/1",
            "failed_count": "0",
            "archived_count": "0",
            "switch_intent_at": "2026-08-31T03:00:00+00:00",
        },
        verification_deadline="2026-08-31T04:00:00+00:00",
        credential_evidence={
            "backend": "baidu",
            "uploader_identity": "app-key",
            "viewer_identity": "app-key",
            "store_target": "/apps/ava-pitr",
            "object_prefix": "ava-pitr",
            "backup_key_id": "key",
            "backup_key_sha256": "0" * 64,
        },
    )
