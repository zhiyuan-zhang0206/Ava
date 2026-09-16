# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""The logical namespace in the four retention inventory readers (P2).

One case per backend: strict naming is the gate, the sidecar pair (where
the backend has one) is the full-strength binding, and anything
unresolvable is surfaced as unknown rather than silently skipped. The
policy-side consumption lives in ``test_pitr_logical_retention``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from services.pitr.baidu_inventory import BaiduRetentionInventoryReader
from services.pitr.checksums import CRC32C, MD5
from services.pitr.cos_inventory import CosRetentionInventoryReader
from services.pitr.logical_dump_names import REMOTE_ROOT
from services.pitr.retention_inventory import (
    LOGICAL_NAMESPACE,
    GCSRetentionInventoryReader,
)
from tests.services.baidu_test_support import (
    APP_ROOT,
    FakePcs,
    FakeTokenManager,
    pcs_client_for,
)
from tests.services.cos_test_support import FakeCos, cos_client_for
from tests.services.oss_test_support import FakeOssBucket, make_inventory
from tests.services.test_pitr_retention_sidecars import _oss_sidecar_bytes

_DAILY = f"{REMOTE_ROOT}/ava-20260916T030000Z.dump.enc"


def test_oss_logical_inventory_reads_pairs_single_puts_and_unknowns() -> None:
    fake = FakeOssBucket()
    payload = b"multipart-ciphertext"
    host_etag = fake.seed(_DAILY, data=payload, multipart=True)
    sidecar_etag = fake.seed(
        f"{_DAILY}.ack.json", data=_oss_sidecar_bytes(_DAILY, payload, host_etag)
    )

    weak = f"{REMOTE_ROOT}/ava-20260915T030000Z.pre-update.dump.enc"
    fake.seed(weak, data=b"single-put")  # a single PUT carries its MD5 ETag

    orphan = f"{REMOTE_ROOT}/ava-20260901T030000Z.dump.enc"
    fake.seed(f"{orphan}.ack.json", data=_oss_sidecar_bytes(orphan, b"gone", "opaque:pin"))

    stranded = f"{REMOTE_ROOT}/ava-20260914T030000Z.dump.enc"
    fake.seed(stranded, data=b"x" * 64, multipart=True)  # multipart with no sidecar

    foreign = f"{REMOTE_ROOT}/notes.txt"
    fake.seed(foreign, data=b"foreign")

    snapshot = make_inventory(fake, prefix=REMOTE_ROOT, namespace=LOGICAL_NAMESPACE).snapshot()

    assert [item.object_name for item in snapshot.objects] == [weak, _DAILY]
    assert snapshot.objects[1].kind == "logical"
    assert snapshot.objects[1].checksum_value == hashlib.md5(payload).hexdigest()  # noqa: S324 — fixture digest
    assert [pair.sidecar.object_name for pair in snapshot.sidecar_pairs] == [f"{_DAILY}.ack.json"]
    pair = snapshot.sidecar_pairs[0]
    assert pair.host_pin_token == host_etag
    assert pair.sidecar.pin_token == sidecar_etag
    assert [item.sidecar.object_name for item in snapshot.orphan_sidecars] == [f"{orphan}.ack.json"]
    assert snapshot.orphan_sidecars[0].host.kind == "logical"
    assert snapshot.unknown_names == (stranded, foreign)


class _BlobRow:
    def __init__(
        self,
        name: str,
        *,
        generation: int = 1,
        size: int | None = None,
        crc32c: str | None = "AAAAAA==",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.generation = generation
        self.size = len(name) if size is None else size
        self.crc32c = crc32c
        self.metadata = metadata


class _FakeGcsBucket:
    def __init__(self, rows: list[_BlobRow]) -> None:
        self._rows = rows

    def list_blobs(self, *, prefix: str) -> list[_BlobRow]:
        return [row for row in self._rows if row.name.startswith(prefix)]


def test_gcs_logical_inventory_reads_strict_names_and_reports_unknowns() -> None:
    missing_crc = f"{REMOTE_ROOT}/ava-20260915T030000Z.dump.enc"
    foreign = f"{REMOTE_ROOT}/notes.txt"
    bucket = _FakeGcsBucket(
        [
            _BlobRow(
                _DAILY, generation=7, size=10, metadata={"ava-artifact-kind": "logical-backup"}
            ),
            _BlobRow(missing_crc, generation=8, size=10, crc32c=None),
            _BlobRow(foreign, generation=9, size=10),
        ]
    )
    reader = GCSRetentionInventoryReader.from_bucket(
        bucket, prefix=REMOTE_ROOT, namespace=LOGICAL_NAMESPACE
    )

    snapshot = reader.snapshot()

    assert [item.object_name for item in snapshot.objects] == [_DAILY]
    row = snapshot.objects[0]
    assert row.kind == "logical"
    assert row.archive_name is None
    assert (row.pin_token, row.checksum_algo, row.checksum_value) == ("7", CRC32C, "AAAAAA==")
    assert snapshot.unknown_names == (missing_crc, foreign)
    assert snapshot.sidecar_pairs == () and snapshot.orphan_sidecars == ()


def test_cos_logical_inventory_reads_single_put_rows_and_rejects_composites() -> None:
    fake = FakeCos()
    keep_etag = fake.seed(_DAILY, b"ciphertext")
    composite = f"{REMOTE_ROOT}/ava-20260915T030000Z.pre-update.dump.enc"
    fake.seed(composite, b"snapshot")
    fake.etag_overrides[composite] = "composite-2"  # a multipart ETag is not adoptable
    foreign = f"{REMOTE_ROOT}/notes.txt"
    fake.seed(foreign, b"foreign")

    reader = CosRetentionInventoryReader.from_client(
        cos_client_for(fake), prefix=REMOTE_ROOT, namespace=LOGICAL_NAMESPACE
    )
    snapshot = reader.snapshot()

    assert [item.object_name for item in snapshot.objects] == [_DAILY]
    assert snapshot.objects[0].kind == "logical"
    assert snapshot.objects[0].pin_token == keep_etag
    assert snapshot.unknown_names == (composite, foreign)


def test_baidu_logical_inventory_reads_pairs_and_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePcs()
    reader = BaiduRetentionInventoryReader(
        app_root=APP_ROOT,
        prefix=REMOTE_ROOT,
        token_manager=FakeTokenManager(),
        namespace=LOGICAL_NAMESPACE,
    )
    monkeypatch.setattr(reader._store, "_client", lambda: pcs_client_for(fake))

    sidecar = {
        "object_name": _DAILY,
        "pin_token": "5:opaque",
        "size": 10,
        "checksum_algo": MD5,
        "checksum_value": "m",
        "metadata": {},
    }
    sidecar_data = json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(f"{APP_ROOT}/{_DAILY}", size=10, md5="m")
    fake.seed_file(
        f"{APP_ROOT}/{_DAILY}.ack.json",
        size=len(sidecar_data),
        md5="side-digest",
        dlink="https://dl.test/logical",
    )

    orphan = f"{REMOTE_ROOT}/ava-20260901T030000Z.dump.enc"
    orphan_data = json.dumps(
        {**sidecar, "object_name": orphan, "pin_token": "9:opaque"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fake.seed_file(
        f"{APP_ROOT}/{orphan}.ack.json",
        size=len(orphan_data),
        md5="orphan-digest",
        dlink="https://dl.test/orphan",
    )

    bodies = {"https://dl.test/logical": sidecar_data, "https://dl.test/orphan": orphan_data}

    def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, content=bodies[url.split("&", 1)[0]])

    monkeypatch.setattr(httpx, "get", fake_get)

    snapshot = reader.snapshot()

    assert snapshot.unknown_names == ()
    assert [item.object_name for item in snapshot.objects] == [_DAILY]
    row = snapshot.objects[0]
    assert (row.kind, row.archive_name, row.pin_token, row.size) == (
        "logical",
        None,
        "5:opaque",
        10,
    )
    assert [pair.sidecar.object_name for pair in snapshot.sidecar_pairs] == [f"{_DAILY}.ack.json"]
    assert [item.sidecar.object_name for item in snapshot.orphan_sidecars] == [f"{orphan}.ack.json"]
    assert snapshot.orphan_sidecars[0].host.kind == "logical"
