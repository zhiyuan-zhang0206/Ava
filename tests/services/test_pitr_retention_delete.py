# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Contention-delete role contracts: the GCS adapter's generation pinning,
the OSS adapter's emulated conditional delete, the executor's refusal and
limit matrix, and the append-only journal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from google.api_core.exceptions import DeadlineExceeded, Forbidden, NotFound, PreconditionFailed

from services.pitr.checksums import MD5
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.retention_delete import DeleteOutcome, GCSRetentionDeleteStore
from services.pitr.retention_executor import (
    RetentionExecutionLimits,
    RetentionExecutionSummary,
    execute_retention_plan,
)
from services.pitr.retention_journal import RetentionJournal
from services.pitr.retention_manifest import (
    PLAN_SCHEMA_VERSION,
    RetentionDecision,
    RetentionObject,
    RetentionPlan,
)
from tests.services.oss_test_support import FakeOssBucket, make_delete_store


class _FakeDeleteBlob:
    def __init__(self, bucket: _FakeDeleteBucket, name: str) -> None:
        self._bucket = bucket
        self._name = name

    def delete(self, **kwargs: object) -> None:
        if self._bucket.error is not None:
            raise self._bucket.error
        generation = kwargs["if_generation_match"]
        live = self._bucket.objects.get(self._name)
        if live is None:
            raise NotFound("object is gone")
        if live != generation:
            raise PreconditionFailed("generation differs")
        del self._bucket.objects[self._name]


class _FakeDeleteBucket:
    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = dict(objects)
        self.error: Exception | None = None

    def blob(self, name: str) -> _FakeDeleteBlob:
        return _FakeDeleteBlob(self, name)


def _gcs_store(objects: dict[str, int]) -> tuple[GCSRetentionDeleteStore, _FakeDeleteBucket]:
    bucket = _FakeDeleteBucket(objects)
    return GCSRetentionDeleteStore.from_bucket_client(cast(Any, bucket)), bucket


def test_gcs_delete_if_match_deletes_only_the_pinned_generation() -> None:
    store, bucket = _gcs_store({"a": 7, "b": 8})

    assert store.delete_if_match("a", "7") is DeleteOutcome.DELETED
    assert bucket.objects == {"b": 8}


def test_gcs_delete_if_match_classifies_absent_and_mismatch() -> None:
    store, bucket = _gcs_store({"a": 8})

    assert store.delete_if_match("missing", "7") is DeleteOutcome.ABSENT
    assert store.delete_if_match("a", "7") is DeleteOutcome.MISMATCH
    assert bucket.objects == {"a": 8}


def test_gcs_delete_if_match_rejects_bad_identity_and_maps_errors() -> None:
    store, bucket = _gcs_store({"a": 7})

    with pytest.raises(PermanentObjectStoreError):
        store.delete_if_match("a", "not-a-generation")

    bucket.error = DeadlineExceeded("injected")
    with pytest.raises(TransientObjectStoreError):
        store.delete_if_match("a", "7")

    bucket.error = Forbidden("injected")
    with pytest.raises(PermanentObjectStoreError):
        store.delete_if_match("a", "7")


def test_oss_delete_if_match_requires_the_live_etag() -> None:
    fake = FakeOssBucket()
    name = "ava-pitr-cutover/wal/000000010000000000000001.enc"
    etag = fake.seed(name, data=b"ciphertext")
    store = make_delete_store(fake)

    assert store.delete_if_match(name, etag) is DeleteOutcome.DELETED
    assert name not in fake.files


def test_oss_delete_if_match_refuses_changed_and_classifies_absent() -> None:
    fake = FakeOssBucket()
    name = "ava-pitr-cutover/wal/000000010000000000000001.enc"
    fake.seed(name, data=b"ciphertext")
    store = make_delete_store(fake)

    assert store.delete_if_match(name, "0" * 32) is DeleteOutcome.MISMATCH
    assert name in fake.files
    assert store.delete_if_match("missing.enc", "0" * 32) is DeleteOutcome.ABSENT


def test_oss_delete_if_match_maps_delete_failures() -> None:
    fake = FakeOssBucket()
    name = "ava-pitr-cutover/wal/000000010000000000000001.enc"
    etag = fake.seed(name, data=b"ciphertext")
    store = make_delete_store(fake)

    fake.delete_error = (503, "ServiceUnavailable")
    with pytest.raises(TransientObjectStoreError):
        store.delete_if_match(name, etag)

    fake.delete_error = (403, "AccessDenied")
    with pytest.raises(PermanentObjectStoreError):
        store.delete_if_match(name, etag)


def _object(name: str, *, size: int = 10, pin: str = "pin") -> RetentionObject:
    return RetentionObject(name, pin, size, None, "wal", MD5, "c" * 32, ())


def _plan(*objects: RetentionObject, blocked: tuple[str, ...] = ()) -> RetentionPlan:
    decisions = tuple(
        RetentionDecision(item, "strictly before the oldest retained frontier") for item in objects
    )
    eligible = () if blocked else decisions
    return RetentionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        retained_chain_count=2,
        evidence_sha256="evidence",
        protected_chain_ids=("chain-a",),
        unprotected_chain_ids=(),
        oldest_retained_chain_id="chain-a",
        ack_high_water=None,
        blocked_reasons=blocked,
        retained=(),
        eligible=eligible,
        retained_bytes=0,
        eligible_bytes=sum(item.object.size for item in eligible),
    )


class _FakeDeleteStore:
    def __init__(self, outcomes: dict[str, DeleteOutcome | Exception] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._outcomes = outcomes or {}

    def delete_if_match(self, object_name: str, identity: str) -> DeleteOutcome:
        self.calls.append((object_name, identity))
        outcome = self._outcomes.get(object_name, DeleteOutcome.DELETED)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Gone:
    def __init__(self, *, present: set[str] | None = None) -> None:
        self.present = present or set()
        self.checked: list[str] = []

    def __call__(self, object_name: str) -> bool:
        self.checked.append(object_name)
        return object_name not in self.present


def _journal(tmp_path: Path) -> RetentionJournal:
    return RetentionJournal(tmp_path / "retention-journal")


def _records(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "retention-journal" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_executor_deletes_eligible_objects_and_journals_each_step(tmp_path: Path) -> None:
    plan = _plan(_object("a", size=3), _object("b", size=4, pin="pin-b"))
    store = _FakeDeleteStore()
    gone = _Gone()

    summary = execute_retention_plan(
        plan,
        expected_digest=plan.digest(),
        delete_store=store,
        verify_absent=gone,
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
    )

    assert summary == RetentionExecutionSummary(plan.digest(), None, 2, 2, 0, 0, 0, 0, 0)
    assert store.calls == [("a", "pin"), ("b", "pin-b")]
    assert gone.checked == ["a", "b"]
    records = _records(tmp_path)
    assert [record["kind"] for record in records] == [
        "intent",
        "result",
        "intent",
        "result",
        "tick",
    ]
    assert records[0]["object_name"] == "a"
    assert records[0]["identity"] == "pin"
    assert records[1]["outcome"] == "deleted"
    assert records[-1]["deleted"] == 2


def test_executor_refuses_any_digest_or_blocker_doubt(tmp_path: Path) -> None:
    plan = _plan(_object("a"))
    store = _FakeDeleteStore()

    refused = execute_retention_plan(
        plan,
        expected_digest="f" * 64,
        delete_store=store,
        verify_absent=_Gone(),
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
    )
    assert refused.refused_reason is not None
    assert refused.attempted == 0
    assert store.calls == []
    refused_records = _records(tmp_path)
    assert [record["kind"] for record in refused_records] == ["refused"]

    blocked = _plan(_object("a"), blocked=("stale local ACK",))
    again = execute_retention_plan(
        blocked,
        expected_digest=blocked.digest(),
        delete_store=store,
        verify_absent=_Gone(),
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
    )
    assert again.refused_reason is not None
    assert store.calls == []
    assert [record["kind"] for record in _records(tmp_path)] == ["refused", "refused"]


def test_executor_classifies_mismatch_failure_absent_and_verify_failure(tmp_path: Path) -> None:
    plan = _plan(
        _object("a"),
        _object("b"),
        _object("c"),
        _object("d"),
        _object("e"),
    )
    store = _FakeDeleteStore(
        {
            "b": DeleteOutcome.MISMATCH,
            "c": PermanentObjectStoreError("injected"),
            "e": DeleteOutcome.ABSENT,
        }
    )
    gone = _Gone(present={"d"})

    summary = execute_retention_plan(
        plan,
        expected_digest=plan.digest(),
        delete_store=store,
        verify_absent=gone,
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
    )

    assert summary.attempted == 5
    assert summary.deleted == 1
    assert summary.absent == 1
    assert summary.mismatched == 1
    assert summary.failed == 1
    assert summary.verify_failed == 1
    assert gone.checked == ["a", "d"]
    outcomes = [record["outcome"] for record in _records(tmp_path) if record["kind"] == "result"]
    assert outcomes == ["deleted", "identity-mismatch", "delete-failed", "verify-failed", "absent"]


def test_executor_limits_objects_bytes_and_rate(tmp_path: Path) -> None:
    plan = _plan(_object("a", size=3), _object("b", size=3), _object("c", size=3))

    capped = execute_retention_plan(
        plan,
        expected_digest=plan.digest(),
        delete_store=_FakeDeleteStore(),
        verify_absent=_Gone(),
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
        limits=RetentionExecutionLimits(max_objects=2),
    )
    assert (capped.attempted, capped.skipped) == (2, 1)

    byte_plan = _plan(_object("a", size=3), _object("b", size=3))
    byte_capped = execute_retention_plan(
        byte_plan,
        expected_digest=byte_plan.digest(),
        delete_store=_FakeDeleteStore(),
        verify_absent=_Gone(),
        remote_total_bytes=100,
        journal=_journal(tmp_path),
    )
    assert (byte_capped.attempted, byte_capped.skipped) == (1, 1)

    oversized = _plan(_object("big", size=10))
    skipped_store = _FakeDeleteStore()
    oversized_summary = execute_retention_plan(
        oversized,
        expected_digest=oversized.digest(),
        delete_store=skipped_store,
        verify_absent=_Gone(),
        remote_total_bytes=100,
        journal=_journal(tmp_path),
    )
    assert (oversized_summary.attempted, oversized_summary.skipped) == (0, 1)
    assert skipped_store.calls == []

    now = [0.0]
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    paced = execute_retention_plan(
        plan,
        expected_digest=plan.digest(),
        delete_store=_FakeDeleteStore(),
        verify_absent=_Gone(),
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
        limits=RetentionExecutionLimits(rate_per_second=2.0),
        clock=lambda: now[0],
        sleep=_sleep,
    )
    assert paced.attempted == 3
    assert sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


def test_executor_deletes_through_the_oss_role_end_to_end(tmp_path: Path) -> None:
    fake = FakeOssBucket()
    name = "ava-pitr-cutover/wal/000000010000000000000001.enc"
    etag = fake.seed(name, data=b"ciphertext")
    plan = _plan(_object(name, pin=etag, size=16))

    summary = execute_retention_plan(
        plan,
        expected_digest=plan.digest(),
        delete_store=make_delete_store(fake),
        verify_absent=lambda object_name: object_name not in fake.files,
        remote_total_bytes=1000,
        journal=_journal(tmp_path),
    )

    assert summary.deleted == 1
    assert name not in fake.files


def test_journal_appends_fsynced_jsonl_records(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.append("arm", {"actor": "ava-5215", "plan_digest": "d"})
    journal.append("disable", {"actor": "ava-5215"})

    path = tmp_path / "retention-journal" / "journal.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["arm", "disable"]
    assert records[0]["actor"] == "ava-5215"
    assert all(record["at"] for record in records)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
