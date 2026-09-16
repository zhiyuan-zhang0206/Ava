"""Pure, fail-closed retention policy for dry-run plans.

Two surfaces, one plan: the PITR prefix's N-chain rules and the flat
logical dump namespace (``ava-logical/``), whose window mirrors the local
pool's ``services.backup._prune``. Uncertainty on either surface yields
zero eligibility.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, tzinfo

from services.pitr.base_manifest import CandidateManifest, _lsn
from services.pitr.logical_dump_names import (
    KIND_ACTIVATION,
    KIND_DAILY,
    KIND_PRE_UPDATE,
    KINDS,
    LogicalDumpName,
    parse_dump_name,
    relative_name,
    stamp_utc,
)
from services.pitr.restore_manifest import ProtectedManifest, required_archive_names
from services.pitr.retention_manifest import (
    PLAN_SCHEMA_VERSION,
    OrphanSidecar,
    RetentionDecision,
    RetentionObject,
    RetentionPlan,
    RetentionSidecar,
    SidecarPair,
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
    sidecar_pairs: tuple[SidecarPair, ...] = ()
    orphan_sidecars: tuple[OrphanSidecar, ...] = ()
    logical_inventory: tuple[RetentionObject, ...] = ()
    logical_sidecar_pairs: tuple[SidecarPair, ...] = ()
    logical_orphan_sidecars: tuple[OrphanSidecar, ...] = ()


@dataclass(frozen=True)
class LogicalRetention:
    """The logical namespace's retention window.

    The defaults mirror the local pool's field defaults (``services.backup_keep``
    and ``services.backup.ACTIVATION_KEEP``); production callers pass the
    writer's live values so the off-site mirror cannot drift. ``legacy_tz`` is the
    cluster wall clock legacy (offset-less) stamps are read in;
    ``active_pin_name`` is the in-flight activation operation's pinned
    snapshot, kept regardless of the window.
    """

    keep_dailies: int = 7
    keep_pre_updates: int = 1
    keep_activations: int = 2
    legacy_tz: tzinfo | None = None
    active_pin_name: str | None = None

    def __post_init__(self) -> None:
        if min(self.keep_dailies, self.keep_pre_updates, self.keep_activations) < 1:
            raise ValueError("logical retention depths must be positive")


def plan_retention(  # noqa: PLR0915
    evidence: RetentionEvidence,
    *,
    retain_chains: int = 2,
    logical_retention: LogicalRetention | None = None,
) -> RetentionPlan:
    """Return a deterministic dry-run plan; uncertainty always yields zero eligibility.

    ``logical_retention`` enables the logical-namespace half of the plan;
    None (the legacy call shape) leaves that namespace out of scope.
    """

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
            blockers.add("ambiguous protected proof pin token")
            continue
        protected[proof.chain_id] = proof

    ordered = sorted(protected, key=capture_times.__getitem__)
    retained_chains = ordered[-retain_chains:]
    unprotected_chains = sorted(set(candidates) - set(protected))
    if unprotected_chains:
        blockers.add("unprotected candidate exists")
    if len(retained_chains) < retain_chains:
        blockers.add("fewer than two protected chains")

    remote_inventory = {(item.object_name, item.pin_token): item for item in evidence.inventory}
    if len(remote_inventory) != len(evidence.inventory):
        blockers.add("duplicate remote object pin token")
    by_name: dict[str, set[str]] = {}
    for item in evidence.inventory:
        by_name.setdefault(item.object_name, set()).add(item.pin_token)
    if any(len(tokens) != 1 for tokens in by_name.values()):
        blockers.add("ambiguous remote object pin token")

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
        verified_inventory[(remote.object_name, remote.pin_token)] = remote
    cross_timeline = any(item.kind == "history" for item in evidence.inventory) or any(
        wal_range.timeline > 1 for item in candidates.values() for wal_range in item.wal_ranges
    )
    if cross_timeline:
        blockers.add("cross-timeline ancestry is not authenticated by this planner")

    keep: dict[tuple[str, str], str] = {}
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
        (proof.base.object_name, proof.base.pin_token)
        for chain_id, proof in protected.items()
        if chain_id not in retained_chains
    }
    pair_by_host = _sidecar_pairs_by_host(evidence.sidecar_pairs, blockers)
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

    orphan_sidecars = _eligible_orphan_sidecars(
        evidence.orphan_sidecars, protected_objects, oldest, candidates, blockers
    )
    logical_retained: list[RetentionDecision] = []
    logical_eligible: list[RetentionDecision] = []
    logical_orphans: list[RetentionSidecar] = []
    logical_pair_by_host: dict[str, SidecarPair] = {}
    if logical_retention is not None:
        logical_pair_by_host = _sidecar_pairs_by_host(evidence.logical_sidecar_pairs, blockers)
        logical_retained, logical_eligible, logical_orphans = _plan_logical_namespace(
            evidence, logical_retention, blockers
        )
    if blockers:
        retained.extend(eligible)
        eligible = []
        # Fold each surface into its own decision bucket: the sidecar indexes
        # are per-surface, so a logical decision must not ride the physical list.
        logical_retained.extend(logical_eligible)
        logical_eligible = []
        orphan_sidecars = []
        logical_orphans = []
    retained = _canonical_decisions(
        [_attach_sidecar(item, pair_by_host) for item in retained]
        + [_attach_sidecar(item, logical_pair_by_host) for item in logical_retained]
    )
    eligible = _canonical_decisions(
        [_attach_sidecar(item, pair_by_host) for item in eligible]
        + [_attach_sidecar(item, logical_pair_by_host) for item in logical_eligible]
    )
    weak_evidence = tuple(
        sorted(
            item.object.object_name
            for item in (*retained, *eligible)
            if item.object.kind == "logical" and item.sidecar is None
        )
    )
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
        tuple(sorted(orphan_sidecars + logical_orphans)),
        weak_evidence,
    )


def _pin_candidate(
    keep: dict[tuple[str, str], str],
    candidate: CandidateManifest,
    inventory: dict[tuple[str, str], RetentionObject],
    reason: str,
    blockers: set[str],
) -> None:
    base = (candidate.base_object.object_name, candidate.base_object.pin_token)
    actual_base = inventory.get(base)
    if actual_base is None or (
        actual_base.size,
        actual_base.checksum_algo,
        actual_base.checksum_value,
    ) != (
        candidate.base_object.ciphertext_size,
        candidate.base_object.ciphertext_checksum_algo,
        candidate.base_object.ciphertext_checksum_value,
    ):
        blockers.add("candidate base pin token is missing or changed")
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
    keep: dict[tuple[str, str], str],
    proof: ProtectedManifest,
    inventory: dict[tuple[str, str], RetentionObject],
    reason: str,
    blockers: set[str],
) -> None:
    objects = (proof.base, *proof.wal)
    for expected in objects:
        identity = (expected.object_name, expected.pin_token)
        actual = inventory.get(identity)
        if actual is None or (
            actual.size,
            actual.checksum_algo,
            actual.checksum_value,
            actual.metadata,
        ) != (
            expected.size,
            expected.checksum_algo,
            expected.checksum_value,
            expected.metadata,
        ):
            blockers.add("protected object pin token is missing or changed")
        keep[identity] = reason


_BACKUP_HISTORY_SUFFIX = ".backup"


def _pin_contiguous_wal(
    keep: dict[tuple[str, str], str],
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
        keep[(item.object_name, item.pin_token)] = (
            "timeline ancestry" if item.kind == "history" else "continuous WAL recovery window"
        )
    wal = [item for item in inventory if item.kind == "wal" and item.archive_name is not None]
    latest_range = oldest.wal_ranges[-1]
    timeline = latest_range.timeline
    segment_size = oldest.wal_segment_size
    start = (_lsn(latest_range.end_lsn) + segment_size - 1) // segment_size
    on_timeline: dict[int, RetentionObject] = {}
    for item in wal:
        name = item.archive_name or ""
        if name.endswith(_BACKUP_HISTORY_SUFFIX):
            # A backup-history file anchors the segment in its prefix; it is
            # not a segment itself. Keeping it out of the contiguity map
            # avoids a false `forked` verdict against that segment's real
            # object; the frontier classification owns its retention.
            continue
        item_timeline, segment = _segment(name, segment_size)
        if item_timeline == timeline:
            if segment in on_timeline:
                blockers.add("forked WAL pin token at one segment")
            on_timeline[segment] = item
    current = start
    high_water = next((name for name in reversed(required) if not name.endswith(".history")), None)
    while current in on_timeline:
        item = on_timeline[current]
        keep[(item.object_name, item.pin_token)] = "continuous WAL recovery window"
        high_water = item.archive_name
        current += 1
    if any(segment > current for segment in on_timeline):
        blockers.add("gap before remote ACK high-water")
    for item in inventory:
        if item.kind == "history":
            keep[(item.object_name, item.pin_token)] = "timeline ancestry"
    return high_water


def _segment(name: str, segment_size: int) -> tuple[int, int]:
    if len(name) != 24:
        raise ValueError("WAL archive name is malformed")
    timeline = int(name[:8], 16)
    segments_per_log = 0x100000000 // segment_size
    return timeline, int(name[8:16], 16) * segments_per_log + int(name[16:], 16)


def _segment_origin(name: str, segment_size: int) -> tuple[int, int]:
    """The WAL segment a ``wal/`` archive name anchors.

    A segment name anchors itself; a backup-history file
    (``<segment>.<time>.backup``) anchors the segment in its prefix - it is
    archived beside that segment and is not a segment itself.
    """
    if name.endswith(_BACKUP_HISTORY_SUFFIX):
        name = name[:24]
    return _segment(name, segment_size)


def _before_frontier(item: RetentionObject, oldest: CandidateManifest) -> bool:
    if item.archive_name is None:
        return False
    timeline, segment = _segment_origin(item.archive_name, oldest.wal_segment_size)
    start_timeline = oldest.wal_ranges[0].timeline
    start_segment = _lsn(oldest.start_lsn) // oldest.wal_segment_size
    return timeline < start_timeline or (timeline == start_timeline and segment < start_segment)


def _sidecar_pairs_by_host(
    pairs: tuple[SidecarPair, ...], blockers: set[str]
) -> dict[str, SidecarPair]:
    """Index the observed pairs; a host with two differing sidecars is ambiguous."""

    by_host: dict[str, SidecarPair] = {}
    for pair in pairs:
        host = pair.sidecar.host_name()
        existing = by_host.get(host)
        if existing is not None:
            if existing != pair:
                blockers.add("ambiguous sidecar observation")
            continue
        by_host[host] = pair
    return by_host


def _attach_sidecar(
    decision: RetentionDecision, pair_by_host: dict[str, SidecarPair]
) -> RetentionDecision:
    """Attach the observed sidecar only when it still binds to the live pin token."""

    pair = pair_by_host.get(decision.object.object_name)
    if pair is None or pair.host_pin_token != decision.object.pin_token:
        return decision
    return RetentionDecision(decision.object, decision.reason, pair.sidecar)


def _eligible_orphan_sidecars(
    observations: tuple[OrphanSidecar, ...],
    protected_objects: set[tuple[str, str]],
    oldest: str | None,
    candidates: dict[str, CandidateManifest],
    blockers: set[str],
) -> list[RetentionSidecar]:
    """Keep only orphans whose reconstructed host passes the normal predicates."""

    eligible: list[RetentionSidecar] = []
    seen: dict[str, RetentionSidecar] = {}
    for observation in observations:
        host = observation.host
        existing = seen.get(host.object_name)
        if existing is not None:
            if existing != observation.sidecar:
                blockers.add("ambiguous orphan sidecar observation")
            continue
        seen[host.object_name] = observation.sidecar
        if host.kind == "base":
            if (host.object_name, host.pin_token) in protected_objects:
                eligible.append(observation.sidecar)
        elif (
            host.kind == "wal" and oldest is not None and _before_frontier(host, candidates[oldest])
        ):
            eligible.append(observation.sidecar)
    return eligible


_LOGICAL_WINDOW_REASONS = {
    KIND_DAILY: "logical daily dump inside the retention window",
    KIND_PRE_UPDATE: "logical pre-update snapshot inside the retention window",
    KIND_ACTIVATION: "logical activation snapshot inside the retention window",
}
_LOGICAL_BEYOND_REASONS = {
    KIND_DAILY: "logical daily dump beyond the retention window",
    KIND_PRE_UPDATE: "logical pre-update snapshot beyond the retention window",
    KIND_ACTIVATION: "logical activation snapshot beyond the retention window",
}
_LOGICAL_PIN_REASON = "active recovery floor pin"
_LOGICAL_FAIL_CLOSED_REASON = "logical object pinned fail closed outside the managed grammar"
_LOGICAL_UNREADABLE_REASON = "logical dump pinned fail closed without a readable stamp"
_LOGICAL_LEGACY_BLOCKER = "logical retention cannot read a legacy stamp without a timezone"


def _plan_logical_namespace(
    evidence: RetentionEvidence,
    retention: LogicalRetention,
    blockers: set[str],
) -> tuple[list[RetentionDecision], list[RetentionDecision], list[RetentionSidecar]]:
    """Decide the logical dump namespace, mirroring the local pool's prune.

    The newest ``keep_dailies`` daily dumps plus the newest
    ``keep_pre_updates`` pre-update snapshot plus the newest
    ``keep_activations`` activation snapshots stay; every other object in
    the namespace is eligible. The in-flight activation pin keeps its
    object regardless of the window. A name outside the grammar, an
    unreadable stamp, or an ambiguous identity blocks the plan -- on a
    blocker every object lands in the retained side (fail closed).
    """
    identities = {(item.object_name, item.pin_token) for item in evidence.logical_inventory}
    if len(identities) != len(evidence.logical_inventory):
        blockers.add("duplicate logical object pin token")
    buckets: dict[str, list[tuple[datetime, RetentionObject]]] = {kind: [] for kind in KINDS}
    retained: list[RetentionDecision] = []
    eligible: list[RetentionDecision] = []
    for item in evidence.logical_inventory:
        relative = relative_name(item.object_name)
        parsed = None if relative is None else parse_dump_name(relative)
        if item.kind != "logical" or item.archive_name is not None or parsed is None:
            blockers.add("logical object is outside its managed namespace")
            retained.append(RetentionDecision(item, _LOGICAL_FAIL_CLOSED_REASON))
            continue
        stamp = _logical_stamp(parsed, retention)
        if stamp is None:
            blockers.add(_LOGICAL_LEGACY_BLOCKER)
            retained.append(RetentionDecision(item, _LOGICAL_UNREADABLE_REASON))
            continue
        buckets[parsed.kind].append((stamp, item))
    limits = {
        KIND_DAILY: retention.keep_dailies,
        KIND_PRE_UPDATE: retention.keep_pre_updates,
        KIND_ACTIVATION: retention.keep_activations,
    }
    windows: dict[str, list[tuple[datetime, RetentionObject]]] = {}
    for kind, members in buckets.items():
        ordered = sorted(members, key=lambda pair: (pair[0], pair[1].object_name))
        window = ordered[-limits[kind] :]
        windows[kind] = window
        window_names = {item.object_name for _stamp, item in window}
        for _stamp, item in members:
            if item.object_name in window_names:
                retained.append(RetentionDecision(item, _LOGICAL_WINDOW_REASONS[kind]))
            elif (
                retention.active_pin_name is not None
                and relative_name(item.object_name) == retention.active_pin_name
            ):
                retained.append(RetentionDecision(item, _LOGICAL_PIN_REASON))
            else:
                eligible.append(RetentionDecision(item, _LOGICAL_BEYOND_REASONS[kind]))
    orphans = _eligible_logical_orphans(
        evidence.logical_orphan_sidecars, windows, limits, retention, blockers
    )
    return retained, eligible, orphans


def _logical_stamp(parsed: LogicalDumpName, retention: LogicalRetention) -> datetime | None:
    """The parsed name's stamp, or None when the reading rules are unmet."""
    if parsed.legacy and retention.legacy_tz is None:
        return None
    return stamp_utc(parsed.stamp, retention.legacy_tz)


def _eligible_logical_orphans(
    observations: tuple[OrphanSidecar, ...],
    windows: dict[str, list[tuple[datetime, RetentionObject]]],
    limits: dict[str, int],
    retention: LogicalRetention,
    blockers: set[str],
) -> list[RetentionSidecar]:
    """Keep only orphan sidecars whose gone host is provably beyond the window.

    A logical orphan is eligible only when its host's class still holds the
    full window of live members and the host's stamp is strictly older than
    the oldest of them: with the host present it would rank outside the
    window. Fewer live members than the window -- or any doubt about the
    host's name or stamp -- keeps the sidecar (fail closed).
    """
    eligible: list[RetentionSidecar] = []
    seen: dict[str, RetentionSidecar] = {}
    for observation in observations:
        host = observation.host
        existing = seen.get(host.object_name)
        if existing is not None:
            if existing != observation.sidecar:
                blockers.add("ambiguous orphan sidecar observation")
            continue
        seen[host.object_name] = observation.sidecar
        relative = relative_name(host.object_name)
        parsed = None if relative is None else parse_dump_name(relative)
        if host.kind != "logical" or host.archive_name is not None or parsed is None:
            blockers.add("logical orphan sidecar host is outside its managed namespace")
            continue
        host_stamp = _logical_stamp(parsed, retention)
        if host_stamp is None:
            blockers.add(_LOGICAL_LEGACY_BLOCKER)
            continue
        window = windows[parsed.kind]
        if len(window) != limits[parsed.kind]:
            continue
        if host_stamp < min(stamp for stamp, _item in window):
            eligible.append(observation.sidecar)
    return eligible


def _canonical_decisions(items: list[RetentionDecision]) -> list[RetentionDecision]:
    by_identity: dict[tuple[str, str], RetentionDecision] = {}
    for item in items:
        by_identity[(item.object.object_name, item.object.pin_token)] = item
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
        "sidecar_pairs": [
            json.dumps(asdict(item), sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.sidecar_pairs)
        ],
        "orphan_sidecars": [
            json.dumps(asdict(item), sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.orphan_sidecars)
        ],
        "logical_inventory": [
            json.dumps(item.__dict__, sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.logical_inventory)
        ],
        "logical_sidecar_pairs": [
            json.dumps(asdict(item), sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.logical_sidecar_pairs)
        ],
        "logical_orphan_sidecars": [
            json.dumps(asdict(item), sort_keys=True, separators=(",", ":"))
            for item in sorted(evidence.logical_orphan_sidecars)
        ],
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
