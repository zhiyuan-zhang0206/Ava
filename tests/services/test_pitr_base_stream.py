from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.pitr.base_object_store import RestartableEncryptedSource
from services.pitr.base_stream import load_or_create_source


def _bytes(source: RestartableEncryptedSource) -> bytes:
    return b"".join(source.iter_chunks())


def _candidate(path: Path) -> None:
    path.mkdir()
    (path / "backup_manifest").write_bytes(b"manifest")


def test_restart_reopens_identical_canonical_ciphertext(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.ready"
    _candidate(candidate)
    (candidate / "z").write_bytes(b"last")
    (candidate / "a").write_bytes(b"first")
    plan_path = tmp_path / "plan.json"
    key = b"k" * 32

    first, first_plan = load_or_create_source(
        candidate,
        plan_path=plan_path,
        key=key,
        key_id="key-1",
        object_name="base/one.enc",
    )
    first_bytes = _bytes(first)
    restarted, restarted_plan = load_or_create_source(
        candidate,
        plan_path=plan_path,
        key=key,
        key_id="key-1",
        object_name="base/one.enc",
    )

    assert _bytes(restarted) == first_bytes
    assert restarted_plan == first_plan
    assert len(first_bytes) == first_plan.ciphertext_size
    assert plan_path.stat().st_mode & 0o777 == 0o600


def test_candidate_mutation_invalidates_durable_plan(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.ready"
    _candidate(candidate)
    source_file = candidate / "base"
    source_file.write_bytes(b"before")
    plan_path = tmp_path / "plan.json"
    load_or_create_source(
        candidate,
        plan_path=plan_path,
        key=b"k" * 32,
        key_id="key-1",
        object_name="base/one.enc",
    )
    source_file.write_bytes(b"after")

    with pytest.raises(ValueError, match="does not match"):
        load_or_create_source(
            candidate,
            plan_path=plan_path,
            key=b"k" * 32,
            key_id="key-1",
            object_name="base/one.enc",
        )


def test_candidate_rejects_symlink(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.ready"
    _candidate(candidate)
    (candidate / "escape").symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="unsupported entry"):
        load_or_create_source(
            candidate,
            plan_path=tmp_path / "plan.json",
            key=b"k" * 32,
            key_id="key-1",
            object_name="base/one.enc",
        )


def test_corrupt_durable_crc_fails_before_source_is_returned(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.ready"
    _candidate(candidate)
    (candidate / "base").write_bytes(b"content")
    plan_path = tmp_path / "plan.json"
    load_or_create_source(
        candidate,
        plan_path=plan_path,
        key=b"k" * 32,
        key_id="key-1",
        object_name="base/one.enc",
    )
    plan = json.loads(plan_path.read_text())
    plan["ciphertext_crc32c"] = "AAAAAA=="
    plan_path.write_text(json.dumps(plan))

    with pytest.raises(ValueError, match="does not reproduce"):
        load_or_create_source(
            candidate,
            plan_path=plan_path,
            key=b"k" * 32,
            key_id="key-1",
            object_name="base/one.enc",
        )
