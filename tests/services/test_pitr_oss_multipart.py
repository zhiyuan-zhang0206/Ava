# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Incomplete-multipart surface over the OSS fake (task #3662): the read-only
inventory sizes every pending upload, an upload that vanishes mid-scan drops
out instead of failing the scan, the single-upload abort maps NoSuchUpload to
NOT_FOUND (completed ids can never be aborted), and permission / transport
failures keep the standard transient-permanent taxonomy."""

from __future__ import annotations

from typing import Any, cast

import pytest
from oss2.models import PartInfo

from services.pitr import oss_multipart
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.oss_multipart import AbortOutcome, IncompleteUpload, OSSMultipartUploads
from tests.services.oss_test_support import FakeOssBucket


def make_surface(fake: FakeOssBucket) -> OSSMultipartUploads:
    return OSSMultipartUploads.from_bucket(cast(Any, fake))


def seed_upload(fake: FakeOssBucket, key: str, *, parts: list[bytes], initiated: int) -> str:
    fake.now = initiated
    init = fake.init_multipart_upload(key)
    upload_id = init.upload_id
    assert upload_id is not None
    for number, payload in enumerate(parts, start=1):
        fake.upload_part(key, upload_id, number, payload)
    return upload_id


def test_inventory_lists_and_sizes_every_upload_oldest_first() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    newer = seed_upload(fake, "ava-logical/newer.enc", parts=[b"a" * 11], initiated=1_788_652_900)
    older = seed_upload(
        fake, "ava-logical/older.enc", parts=[b"b" * 7, b"c" * 13], initiated=1_788_652_800
    )

    rows = surface.inventory()

    assert [row.key for row in rows] == ["ava-logical/older.enc", "ava-logical/newer.enc"]
    assert rows[0] == IncompleteUpload(
        key="ava-logical/older.enc",
        upload_id=older,
        initiated=1_788_652_800,
        part_count=2,
        size_bytes=20,
    )
    assert rows[1] == IncompleteUpload(
        key="ava-logical/newer.enc",
        upload_id=newer,
        initiated=1_788_652_900,
        part_count=1,
        size_bytes=11,
    )


def test_inventory_prefix_filters_uploads() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    seed_upload(fake, "ava-wsl-cutover-20260909-d821f0a366/base/x.enc", parts=[b"a"], initiated=1)
    seed_upload(fake, "ava-logical/y.enc", parts=[b"b"], initiated=2)

    rows = surface.inventory(prefix="ava-logical/")

    assert [row.key for row in rows] == ["ava-logical/y.enc"]


def test_inventory_pages_through_uploads_and_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oss_multipart, "_UPLOAD_PAGE_SIZE", 1)
    monkeypatch.setattr(oss_multipart, "_PARTS_PAGE_SIZE", 1)
    fake = FakeOssBucket()
    surface = make_surface(fake)
    first = seed_upload(fake, "k1", parts=[b"a", b"bb"], initiated=10)
    second = seed_upload(fake, "k2", parts=[b"ccc"], initiated=20)

    rows = surface.inventory()

    assert rows == [
        IncompleteUpload(key="k1", upload_id=first, initiated=10, part_count=2, size_bytes=3),
        IncompleteUpload(key="k2", upload_id=second, initiated=20, part_count=1, size_bytes=3),
    ]


def test_inventory_drops_upload_that_vanishes_mid_scan() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    upload_id = seed_upload(fake, "ava-logical/gone.enc", parts=[b"a"], initiated=1)
    fake.parts_error = (404, "NoSuchUpload")

    assert surface.inventory() == []
    assert surface.find(key="ava-logical/gone.enc", upload_id=upload_id) is None


def test_find_matches_only_the_exact_key_and_upload_id() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    upload_id = seed_upload(fake, "ava-logical/x.enc", parts=[b"z"], initiated=5)

    found = surface.find(key="ava-logical/x.enc", upload_id=upload_id)
    assert found == IncompleteUpload(
        key="ava-logical/x.enc", upload_id=upload_id, initiated=5, part_count=1, size_bytes=1
    )
    assert surface.find(key="ava-logical/x.enc", upload_id="up999") is None
    assert surface.find(key="ava-logical/other.enc", upload_id=upload_id) is None


def test_abort_removes_only_the_targeted_upload() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    first = seed_upload(fake, "k-a", parts=[b"a"], initiated=1)
    seed_upload(fake, "k-b", parts=[b"b"], initiated=2)

    assert surface.abort(key="k-a", upload_id=first) is AbortOutcome.ABORTED

    assert [row.key for row in surface.inventory()] == ["k-b"]


def test_abort_of_completed_or_unknown_upload_is_not_found() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    assert surface.abort(key="missing.enc", upload_id="up404") is AbortOutcome.NOT_FOUND

    fake.now = 5
    init = fake.init_multipart_upload("ava-logical/done.enc")
    upload_id = init.upload_id
    assert upload_id is not None
    part = fake.upload_part("ava-logical/done.enc", upload_id, 1, b"payload")
    assert part.etag is not None
    fake.complete_multipart_upload("ava-logical/done.enc", upload_id, [PartInfo(1, part.etag)])

    # A completed upload's id is gone; a late abort touches nothing.
    assert surface.abort(key="ava-logical/done.enc", upload_id=upload_id) is AbortOutcome.NOT_FOUND


def test_access_denied_maps_to_permanent() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    fake.list_uploads_error = (403, "AccessDenied")

    with pytest.raises(PermanentObjectStoreError):
        surface.inventory()


def test_transport_failure_maps_to_transient() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    fake.request_error = True

    with pytest.raises(TransientObjectStoreError):
        surface.inventory()


def test_abort_rejection_maps_to_permanent() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    fake.abort_error = (403, "AccessDenied")

    with pytest.raises(PermanentObjectStoreError):
        surface.abort(key="k", upload_id="up1")


def test_abort_transport_failure_maps_to_transient() -> None:
    fake = FakeOssBucket()
    surface = make_surface(fake)
    fake.request_error = True

    with pytest.raises(TransientObjectStoreError):
        surface.abort(key="k", upload_id="up1")
