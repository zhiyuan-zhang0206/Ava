from __future__ import annotations

from dataclasses import replace

from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.restore_manifest import (
    ProtectedManifest,
    RestoreObject,
    RestoreProof,
    candidate_sha256,
    required_archive_names,
)
from services.pitr.retention_manifest import RetentionObject, RetentionPlan
from services.pitr.retention_policy import RetentionEvidence, plan_retention

SEGMENT = 16 * 1024 * 1024


def _candidate(chain: str, start: int, end: int, *, timeline: int = 1) -> CandidateManifest:
    return CandidateManifest(
        1,
        chain,
        False,
        17,
        "ava",
        "42",
        SEGMENT,
        timeline,
        f"0/{start:X}",
        f"0/{end:X}",
        (WalRange(timeline, f"0/{start:X}", f"0/{end:X}"),),
        BaseObject(
            f"pitr/base/{chain}/base", int(chain[-3:-1]), 100, "crc", "sha", 90, "key", "AVAPITRB1"
        ),
        "native",
        "backup_manifest",
        f"pitr/base/{chain}/base",
        int(chain[-3:-1]),
        "migrations",
    )


def _wal_name(segment: int, *, timeline: int = 1) -> str:
    return f"{timeline:08X}00000000{segment:08X}"


def _remote_wal(
    segment: int, *, timeline: int = 1, generation: int | None = None
) -> RetentionObject:
    name = _wal_name(segment, timeline=timeline)
    return RetentionObject(
        f"pitr/wal/{timeline:08X}/{name}.enc",
        segment if generation is None else generation,
        20,
        name,
        "wal",
        "crc",
        _metadata(name),
    )


def _metadata(name: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            {
                "ava-archive-name": name,
                "ava-source-sha256": "source",
                "ava-source-size": "10",
                "ava-ciphertext-crc32c": "crc",
                "ava-encryption-format": "AVAPITR1",
                "ava-key-id": "key",
            }.items()
        )
    )


def _proof(candidate: CandidateManifest) -> ProtectedManifest:
    wal: list[RestoreObject] = []
    for name in required_archive_names(candidate.wal_ranges, SEGMENT):
        if name.endswith(".history"):
            item = RetentionObject(
                f"pitr/wal/{name[:8]}/{name}.enc",
                100 + candidate.timeline,
                20,
                name,
                "history",
                "crc",
                _metadata(name),
            )
        else:
            segment = int(name[16:], 16)
            item = _remote_wal(segment, timeline=candidate.timeline)
        wal.append(
            RestoreObject(
                item.archive_name or "",
                item.object_name,
                item.generation,
                item.size,
                item.crc32c,
                item.metadata,
            )
        )
    return ProtectedManifest(
        1,
        True,
        candidate.chain_id,
        candidate_sha256(candidate),
        candidate,
        RestoreObject(
            "base.tar.zst.enc",
            candidate.base_object.object_name,
            candidate.base_object.generation,
            candidate.base_object.ciphertext_size,
            "crc",
            (),
        ),
        tuple(wal),
        candidate.end_lsn,
        SEGMENT,
        RestoreProof(
            "run",
            "2026-08-29T00:00:00+00:00",
            "2026-08-29T00:01:00+00:00",
            candidate.end_lsn,
            candidate.end_lsn,
            1,
            "live",
            "native",
            1,
            1,
            1,
            1,
            "restored",
        ),
    )


def _inventory(*candidates: CandidateManifest, tail: int = 5) -> tuple[RetentionObject, ...]:
    bases = tuple(
        RetentionObject(
            item.base_object.object_name,
            item.base_object.generation,
            item.base_object.ciphertext_size,
            None,
            "base",
            item.base_object.ciphertext_crc32c,
            (),
        )
        for item in candidates
    )
    return bases + tuple(_remote_wal(segment) for segment in range(1, tail + 1))


def _evidence(
    candidates: tuple[CandidateManifest, ...],
    proofs: tuple[ProtectedManifest, ...],
    inventory: tuple[RetentionObject, ...],
    malformed: tuple[str, ...] = (),
    before: str = "",
    after: str = "",
) -> RetentionEvidence:
    return RetentionEvidence(
        candidates,
        proofs,
        tuple(item for item in inventory if item.kind in {"wal", "history"}),
        inventory,
        malformed,
        before,
        after,
    )


def test_keeps_latest_two_by_capture_chain_not_delayed_drill_completion() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    third = _candidate("20260815T000003Z", 3 * SEGMENT, 4 * SEGMENT)
    delayed = replace(
        _proof(first), proof=replace(_proof(first).proof, completed_at="2027-01-01T00:00:00+00:00")
    )
    evidence = _evidence(
        (third, first, second),
        (_proof(third), delayed, _proof(second)),
        _inventory(first, second, third),
    )

    plan = plan_retention(evidence)

    assert plan.oldest_retained_chain_id == second.chain_id
    assert {item.object.object_name for item in plan.eligible} == {
        first.base_object.object_name,
        _remote_wal(1).object_name,
    }


def test_unprotected_candidate_pins_its_base_and_wal() -> None:
    old = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    third = _candidate("20260815T000003Z", 3 * SEGMENT, 4 * SEGMENT)
    plan = plan_retention(
        _evidence(
            (old, second, third),
            (_proof(second), _proof(third)),
            _inventory(old, second, third),
        )
    )
    assert old.chain_id in plan.unprotected_chain_ids
    assert "unprotected candidate exists" in plan.blocked_reasons
    assert plan.eligible == ()
    assert old.base_object.object_name not in {item.object.object_name for item in plan.eligible}
    assert _remote_wal(1).object_name not in {item.object.object_name for item in plan.eligible}


def test_gap_or_generation_replacement_blocks_every_eligible_object() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    third = _candidate("20260815T000003Z", 3 * SEGMENT, 4 * SEGMENT)
    inventory = tuple(
        item for item in _inventory(first, second, third) if item.archive_name != _wal_name(4)
    )
    inventory += (_remote_wal(5), replace(_remote_wal(3), generation=999))
    plan = plan_retention(
        _evidence((first, second, third), tuple(map(_proof, (first, second, third))), inventory)
    )
    assert plan.eligible == ()
    assert "gap before remote ACK high-water" in plan.blocked_reasons
    assert "ambiguous remote object generation" in plan.blocked_reasons


def test_unknown_or_concurrent_snapshot_blocks_fail_closed() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    plan = plan_retention(
        _evidence(
            (first, second),
            (_proof(first), _proof(second)),
            _inventory(first, second),
            ("mystery",),
            "before",
            "after",
        )
    )
    assert plan.eligible == ()
    assert "unknown or malformed evidence exists" in plan.blocked_reasons
    assert "evidence changed during snapshot" in plan.blocked_reasons


def test_later_timeline_history_is_always_ancestry_pinned() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT, timeline=2)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT, timeline=2)
    history = RetentionObject(
        "pitr/wal/00000002/00000002.history.enc",
        30,
        20,
        "00000002.history",
        "history",
        "crc",
        _metadata("00000002.history"),
    )
    proofs = (_proof(first), _proof(second))
    inventory = tuple(
        RetentionObject(
            obj.object_name,
            obj.generation,
            obj.size,
            obj.archive_name,
            "history" if obj.archive_name.endswith(".history") else "wal",
            obj.crc32c,
            obj.metadata,
        )
        for proof in proofs
        for obj in proof.wal
    )
    inventory = (
        *inventory,
        *(
            RetentionObject(
                candidate.base_object.object_name,
                candidate.base_object.generation,
                100,
                None,
                "base",
                candidate.base_object.ciphertext_crc32c,
                (),
            )
            for candidate in (first, second)
        ),
        history,
    )
    plan = plan_retention(_evidence((first, second), proofs, inventory))
    assert "cross-timeline ancestry is not authenticated by this planner" in plan.blocked_reasons
    assert plan.eligible == ()
    assert any(
        item.object == history and item.reason == "cross-timeline ancestry pinned fail closed"
        for item in plan.retained
    )


def test_canonical_plan_round_trips_and_is_deterministic() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    evidence = _evidence(
        (second, first), (_proof(second), _proof(first)), _inventory(second, first)
    )
    left = plan_retention(evidence)
    right = plan_retention(
        _evidence(
            tuple(reversed(evidence.candidates)),
            tuple(reversed(evidence.protected)),
            tuple(reversed(evidence.inventory)),
        )
    )
    assert left.digest() == right.digest()
    assert RetentionPlan.from_json(left.to_json()) == left


def test_local_ack_and_remote_inventory_must_match_every_immutable_field() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    evidence = _evidence(
        (first, second), (_proof(first), _proof(second)), _inventory(first, second)
    )
    conflicted = replace(
        evidence,
        local_acks=(replace(evidence.local_acks[0], crc32c="different"), *evidence.local_acks[1:]),
    )
    plan = plan_retention(conflicted)
    assert plan.eligible == ()
    assert "local ACK conflicts with exact remote archive identity" in plan.blocked_reasons


def test_missing_extra_or_duplicate_archive_identity_blocks() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    evidence = _evidence(
        (first, second), (_proof(first), _proof(second)), _inventory(first, second)
    )
    duplicate = replace(
        evidence.inventory[-1], object_name="pitr/wal/00000001/duplicate.enc", generation=999
    )
    changed = replace(
        evidence,
        local_acks=evidence.local_acks[:-1],
        inventory=(*evidence.inventory, duplicate),
    )
    plan = plan_retention(changed)
    assert plan.eligible == ()
    assert "local ACK and remote archive inventory differ" in plan.blocked_reasons
    assert "duplicate remote archive name" in plan.blocked_reasons


def test_chain_capture_identity_is_strict_and_plan_digest_ignores_snapshot_mtime() -> None:
    first = _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT)
    second = _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT)
    evidence = _evidence(
        (first, second), (_proof(first), _proof(second)), _inventory(first, second)
    )
    left = plan_retention(replace(evidence, snapshot_before="mtime-a", snapshot_after="mtime-a"))
    right = plan_retention(replace(evidence, snapshot_before="mtime-b", snapshot_after="mtime-b"))
    assert left.digest() == right.digest()

    invalid = replace(first, chain_id="2026-08-01")
    blocked = plan_retention(_evidence((invalid, second), (), _inventory(first, second)))
    assert blocked.eligible == ()
    assert "candidate chain identity is not a canonical UTC capture time" in blocked.blocked_reasons


def test_retention_modules_expose_no_delete_surface() -> None:
    import services.pitr.retention_manifest as manifest
    import services.pitr.retention_planner as planner
    import services.pitr.retention_policy as policy

    names = set(dir(manifest)) | set(dir(planner)) | set(dir(policy))
    assert not any("delete" in name.lower() for name in names)
