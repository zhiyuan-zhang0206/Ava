"""Pure, fail-closed N-chain policy for retention dry-run plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from services.pitr.base_manifest import CandidateManifest, _lsn
from services.pitr.restore_manifest import ProtectedManifest, required_archive_names
from services.pitr.retention_manifest import (
    PLAN_SCHEMA_VERSION,
    RetentionDecision,
    RetentionObject,
    RetentionPlan,
)


@dataclass(frozen=True)
class RetentionEvidence:
    candidates: tuple[CandidateManifest, ...]
    protected: tuple[ProtectedManifest, ...]
    local_acks: tuple[RetentionObject, ...]
    inventory: tuple[RetentionObject, ...]
    malformed_names: tuple[str, ...] = ()
    snapshot_before: str = ""
    snapshot_after: str = ""


def plan_retention(  # noqa: PLR0915
    evidence: RetentionEvidence, *, retain_chains: int = 2
) -> RetentionPlan:
    """Return a deterministic dry-run plan; uncertainty always yields zero eligibility."""

    blockers: set[str] = set()
    if retain_chains < 2:
        raise ValueError("PITR retention must keep at least two chains")
    if evidence.snapshot_before != evidence.snapshot_after:
        blockers.add("evidence changed during snapshot")
    if evidence.malformed_names:
        blockers.add("unknown or malformed evidence exists")

    candidates: dict[str, CandidateManifest] = {}
    capture_times: dict[str, datetime] = {}
    for item in evidence.candidates:
        try:
            captured = datetime.strptime(item.chain_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            blockers.add("candidate chain identity is not a canonical UTC capture time")
            continue
        if captured.strftime("%Y%m%dT%H%M%SZ") != item.chain_id:
            blockers.add("candidate chain identity is not canonical")
            continue
        if f"/base/{item.chain_id}/" not in item.base_object.object_name:
            blockers.add("candidate capture identity differs from its base object")
        candidates[item.chain_id] = item
        capture_times[item.chain_id] = captured
    if len(candidates) != len(evidence.candidates):
        blockers.add("duplicate candidate chain identity")
    protected: dict[str, ProtectedManifest] = {}
    for proof in evidence.protected:
        candidate = candidates.get(proof.chain_id)
        if candidate is None or proof.candidate != candidate:
            blockers.add("protected proof lacks its exact candidate")
            continue
        if proof.chain_id in protected and protected[proof.chain_id] != proof:
            blockers.add("ambiguous protected proof generation")
            continue
        protected[proof.chain_id] = proof

    ordered = sorted(protected, key=capture_times.__getitem__)
    retained_chains = ordered[-retain_chains:]
    unprotected_chains = sorted(set(candidates) - set(protected))
    if unprotected_chains:
        blockers.add("unprotected candidate exists")
    if len(retained_chains) < retain_chains:
        blockers.add("fewer than two protected chains")

    remote_inventory = {(item.object_name, item.generation): item for item in evidence.inventory}
    if len(remote_inventory) != len(evidence.inventory):
        blockers.add("duplicate remote object generation")
    by_name: dict[str, set[int]] = {}
    for item in evidence.inventory:
        by_name.setdefault(item.object_name, set()).add(item.generation)
    if any(len(generations) != 1 for generations in by_name.values()):
        blockers.add("ambiguous remote object generation")

    remote_by_archive = _archive_index(evidence.inventory, "remote", blockers)
    local_by_archive = _archive_index(evidence.local_acks, "local ACK", blockers)
    verified_inventory = {
        identity: item for identity, item in remote_inventory.items() if item.kind == "base"
    }
    for archive_name in sorted(set(remote_by_archive) | set(local_by_archive)):
        remote = remote_by_archive.get(archive_name)
        local = local_by_archive.get(archive_name)
        if remote is None or local is None:
            blockers.add("local ACK and remote archive inventory differ")
            continue
        if remote != local:
            blockers.add("local ACK conflicts with exact remote archive identity")
            continue
        verified_inventory[(remote.object_name, remote.generation)] = remote
    cross_timeline = any(item.kind == "history" for item in evidence.inventory) or any(
        wal_range.timeline > 1 for item in candidates.values() for wal_range in item.wal_ranges
    )
    if cross_timeline:
        blockers.add("cross-timeline ancestry is not authenticated by this planner")

    keep: dict[tuple[str, int], str] = {}
    for chain_id in unprotected_chains:
        candidate = candidates[chain_id]
        _pin_candidate(keep, candidate, verified_inventory, "unprotected candidate", blockers)
    for chain_id in retained_chains:
        proof = protected[chain_id]
        _pin_proof(keep, proof, verified_inventory, "retained protected chain", blockers)

    high_water: str | None = None
    oldest = retained_chains[0] if retained_chains else None
    if oldest is not None:
        high_water = _pin_contiguous_wal(
            keep, candidates[oldest], tuple(verified_inventory.values()), blockers=blockers
        )
    if cross_timeline:
        for identity, item in remote_inventory.items():
            if item.kind in {"wal", "history"}:
                keep[identity] = "cross-timeline ancestry pinned fail closed"

    protected_objects = {
        (proof.base.object_name, proof.base.generation)
        for chain_id, proof in protected.items()
        if chain_id not in retained_chains
    }
    eligible: list[RetentionDecision] = []
    retained: list[RetentionDecision] = []
    for identity, item in sorted(remote_inventory.items()):
        reason = keep.get(identity)
        if reason is not None:
            retained.append(RetentionDecision(item, reason))
        elif identity in protected_objects and item.kind == "base":
            eligible.append(RetentionDecision(item, "older drilled base chain"))
        elif (
            item.kind == "wal" and oldest is not None and _before_frontier(item, candidates[oldest])
        ):
            eligible.append(RetentionDecision(item, "WAL precedes oldest retained base"))
        elif item.kind in {"wal", "history"}:
            retained.append(RetentionDecision(item, "outside proven contiguous deletion frontier"))
        else:
            blockers.add("unknown remote object is not policy-owned")
            retained.append(RetentionDecision(item, "unknown object pinned fail closed"))

    if blockers:
        retained.extend(eligible)
        eligible = []
    retained = _canonical_decisions(retained)
    eligible = _canonical_decisions(eligible)
    evidence_sha = hashlib.sha256(_canonical_evidence(evidence).encode()).hexdigest()
    return RetentionPlan(
        PLAN_SCHEMA_VERSION,
        retain_chains,
        evidence_sha,
        tuple(ordered),
        tuple(unprotected_chains),
        oldest,
        high_water,
        tuple(sorted(blockers)),
        tuple(retained),
        tuple(eligible),
        sum(item.object.size for item in retained),
        sum(item.object.size for item in eligible),
    )


def _pin_candidate(
    keep: dict[tuple[str, int], str],
    candidate: CandidateManifest,
    inventory: dict[tuple[str, int], RetentionObject],
    reason: str,
    blockers: set[str],
) -> None:
    base = (candidate.base_object.object_name, candidate.base_object.generation)
    actual_base = inventory.get(base)
    if actual_base is None or (
        actual_base.size,
        actual_base.crc32c,
    ) != (
        candidate.base_object.ciphertext_size,
        candidate.base_object.ciphertext_crc32c,
    ):
        blockers.add("candidate base generation is missing or changed")
    keep[base] = reason
    required = set(required_archive_names(candidate.wal_ranges, candidate.wal_segment_size))
    seen: set[str] = set()
    for identity, item in inventory.items():
        if item.archive_name in required:
            keep[identity] = reason
            seen.add(item.archive_name)
    if seen != required:
        blockers.add("candidate WAL or timeline ancestry is missing")


def _pin_proof(
    keep: dict[tuple[str, int], str],
    proof: ProtectedManifest,
    inventory: dict[tuple[str, int], RetentionObject],
    reason: str,
    blockers: set[str],
) -> None:
    objects = (proof.base, *proof.wal)
    for expected in objects:
        identity = (expected.object_name, expected.generation)
        actual = inventory.get(identity)
        if actual is None or (
            actual.size,
            actual.crc32c,
            actual.metadata,
        ) != (expected.size, expected.crc32c, expected.metadata):
            blockers.add("protected object generation is missing or changed")
        keep[identity] = reason


def _pin_contiguous_wal(
    keep: dict[tuple[str, int], str],
    oldest: CandidateManifest,
    inventory: tuple[RetentionObject, ...],
    *,
    blockers: set[str],
) -> str | None:
    by_archive = {item.archive_name: item for item in inventory if item.archive_name is not None}
    required = required_archive_names(oldest.wal_ranges, oldest.wal_segment_size)
    for name in required:
        item = by_archive.get(name)
        if item is None:
            blockers.add("gap inside oldest retained recovery chain")
            continue
        keep[(item.object_name, item.generation)] = (
            "timeline ancestry" if item.kind == "history" else "continuous WAL recovery window"
        )
    wal = [item for item in inventory if item.kind == "wal" and item.archive_name is not None]
    latest_range = oldest.wal_ranges[-1]
    timeline = latest_range.timeline
    segment_size = oldest.wal_segment_size
    start = (_lsn(latest_range.end_lsn) + segment_size - 1) // segment_size
    on_timeline: dict[int, RetentionObject] = {}
    for item in wal:
        item_timeline, segment = _segment(item.archive_name or "", segment_size)
        if item_timeline == timeline:
            if segment in on_timeline:
                blockers.add("forked WAL generation at one segment")
            on_timeline[segment] = item
    current = start
    high_water = next((name for name in reversed(required) if not name.endswith(".history")), None)
    while current in on_timeline:
        item = on_timeline[current]
        keep[(item.object_name, item.generation)] = "continuous WAL recovery window"
        high_water = item.archive_name
        current += 1
    if any(segment > current for segment in on_timeline):
        blockers.add("gap before remote ACK high-water")
    for item in inventory:
        if item.kind == "history":
            keep[(item.object_name, item.generation)] = "timeline ancestry"
    return high_water


def _segment(name: str, segment_size: int) -> tuple[int, int]:
    if len(name) != 24:
        raise ValueError("WAL archive name is malformed")
    timeline = int(name[:8], 16)
    segments_per_log = 0x100000000 // segment_size
    return timeline, int(name[8:16], 16) * segments_per_log + int(name[16:], 16)


def _before_frontier(item: RetentionObject, oldest: CandidateManifest) -> bool:
    if item.archive_name is None:
        return False
    timeline, segment = _segment(item.archive_name, oldest.wal_segment_size)
    start_timeline = oldest.wal_ranges[0].timeline
    start_segment = _lsn(oldest.start_lsn) // oldest.wal_segment_size
    return timeline < start_timeline or (timeline == start_timeline and segment < start_segment)


def _canonical_decisions(items: list[RetentionDecision]) -> list[RetentionDecision]:
    by_identity: dict[tuple[str, int], RetentionDecision] = {}
    for item in items:
        by_identity[(item.object.object_name, item.object.generation)] = item
    return [by_identity[key] for key in sorted(by_identity)]


def _canonical_evidence(evidence: RetentionEvidence) -> str:
    value = {
        "candidates": sorted(item.to_json() for item in evidence.candidates),
        "protected": sorted(item.to_json() for item in evidence.protected),
        "local_acks": [
            json.dumps(item.__dict__, sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.local_acks)
        ],
        "inventory": [
            json.dumps(item.__dict__, sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.inventory)
        ],
        "malformed_names": sorted(evidence.malformed_names),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _archive_index(
    objects: tuple[RetentionObject, ...], source: str, blockers: set[str]
) -> dict[str, RetentionObject]:
    values: dict[str, RetentionObject] = {}
    for item in objects:
        if item.kind == "base":
            continue
        if item.archive_name is None:
            blockers.add(f"{source} archive lacks a canonical archive name")
            continue
        if item.archive_name in values:
            blockers.add(f"duplicate {source} archive name")
            continue
        values[item.archive_name] = item
    return values
