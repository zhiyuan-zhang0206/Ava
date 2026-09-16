# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""The logical dump namespace in the retention policy (P2).

Locks the window mirroring the local pool's prune (newest seven dailies,
one pre-update snapshot, two activation snapshots), the weak-evidence
annotation, the in-flight activation pin, and the fail-closed orphan
sidecar rule. The PITR-side helpers come from
``test_pitr_retention_policy`` so every case runs beside a healthy
physical plan; assertions scope to ``kind == "logical"`` decisions.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.pitr.logical_dump_names import REMOTE_ROOT, relative_name
from services.pitr.retention_inventory import InventorySnapshot, RetentionInventoryReader
from services.pitr.retention_manifest import (
    OrphanSidecar,
    RetentionDecision,
    RetentionObject,
    RetentionPlan,
    RetentionSidecar,
    SidecarPair,
)
from services.pitr.retention_planner import build_local_evidence, write_dry_run_plan
from services.pitr.retention_policy import LogicalRetention, RetentionEvidence, plan_retention
from tests.services.test_pitr_retention_policy import (
    SEGMENT,
    _candidate,
    _evidence,
    _inventory,
    _proof,
)

_TZ = ZoneInfo("Asia/Shanghai")


def _logical(name: str, *, pin: str | None = None, size: int = 10) -> RetentionObject:
    return RetentionObject(
        f"{REMOTE_ROOT}/{name}", pin or f"pin-{name}", size, None, "logical", "crc32c", "crc", ()
    )


def _daily(day: int) -> RetentionObject:
    return _logical(f"ava-202609{day:02d}T030000Z.dump.enc")


def _pre_update(day: int) -> RetentionObject:
    return _logical(f"ava-202609{day:02d}T030000Z.pre-update.dump.enc")


def _activation(day: int) -> RetentionObject:
    operation = f"00000000-0000-0000-0000-{day:012d}"
    return _logical(f"ava-202609{day:02d}T030000Z.pitr-activation-{operation}.dump.enc")


def _physical(*, unprotected_first: bool = False) -> RetentionEvidence:
    candidates = (
        _candidate("20260801T000001Z", SEGMENT, 2 * SEGMENT),
        _candidate("20260808T000002Z", 2 * SEGMENT, 3 * SEGMENT),
        _candidate("20260815T000003Z", 3 * SEGMENT, 4 * SEGMENT),
    )
    proofs = tuple(_proof(item) for item in candidates)
    if unprotected_first:
        proofs = proofs[1:]
    return _evidence(candidates, proofs, _inventory(*candidates))


def _plan(
    logical: tuple[RetentionObject, ...],
    *,
    retention: LogicalRetention | None = None,
    physical: RetentionEvidence | None = None,
    sidecar_pairs: tuple[SidecarPair, ...] = (),
    orphan_sidecars: tuple[OrphanSidecar, ...] = (),
) -> RetentionPlan:
    evidence = replace(
        physical if physical is not None else _physical(),
        logical_inventory=logical,
        logical_sidecar_pairs=sidecar_pairs,
        logical_orphan_sidecars=orphan_sidecars,
    )
    policy = LogicalRetention(legacy_tz=_TZ) if retention is None else retention
    return plan_retention(evidence, logical_retention=policy)


def _logical_names(decisions: tuple[RetentionDecision, ...]) -> set[str]:
    return {item.object.object_name for item in decisions if item.object.kind == "logical"}


def _reason(plan: RetentionPlan, item: RetentionObject) -> str:
    for decision in (*plan.retained, *plan.eligible):
        if decision.object.object_name == item.object_name:
            return decision.reason
    raise AssertionError(f"no decision for {item.object_name}")


def _bound_pair(host: RetentionObject, *, pin: str = "side-pin", size: int = 5) -> SidecarPair:
    sidecar = RetentionSidecar(f"{host.object_name}.ack.json", pin, size)
    return SidecarPair(host.pin_token, sidecar)


def test_logical_window_mirrors_the_local_prune() -> None:
    dailies = tuple(_daily(day) for day in range(1, 10))
    snapshots = (_pre_update(1), _pre_update(5))
    activations = (_activation(1), _activation(5), _activation(9))

    plan = _plan(dailies + snapshots + activations)

    assert plan.blocked_reasons == ()
    assert _logical_names(plan.eligible) == {
        _daily(1).object_name,
        _daily(2).object_name,
        _pre_update(1).object_name,
        _activation(1).object_name,
    }
    assert _logical_names(plan.retained) == {_daily(day).object_name for day in range(3, 10)} | {
        _pre_update(5).object_name,
        _activation(5).object_name,
        _activation(9).object_name,
    }
    assert _reason(plan, _daily(1)) == "logical daily dump beyond the retention window"
    assert _reason(plan, _daily(3)) == "logical daily dump inside the retention window"
    assert (
        _reason(plan, _pre_update(5)) == "logical pre-update snapshot inside the retention window"
    )
    assert (
        _reason(plan, _activation(9)) == "logical activation snapshot inside the retention window"
    )


def test_logical_window_keeps_a_short_class_whole() -> None:
    plan = _plan((_daily(1), _daily(2), _activation(1)))
    assert _logical_names(plan.eligible) == set()
    assert _logical_names(plan.retained) == {
        _daily(1).object_name,
        _daily(2).object_name,
        _activation(1).object_name,
    }


def test_logical_legacy_stamps_need_the_cluster_timezone() -> None:
    legacy = _logical("ava-20260916-120000.dump.enc")
    plain = _logical("ava-20260916T050000Z.dump.enc")

    unreadable = _plan((legacy, plain), retention=LogicalRetention(legacy_tz=None))
    assert (
        "logical retention cannot read a legacy stamp without a timezone"
        in unreadable.blocked_reasons
    )
    assert unreadable.eligible == ()
    assert legacy.object_name in _logical_names(unreadable.retained)

    readable = _plan((legacy, plain), retention=LogicalRetention(keep_dailies=1, legacy_tz=_TZ))
    # 12:00 Asia/Shanghai is 04:00 UTC, so the legacy dump is the older one.
    assert _logical_names(readable.retained) == {plain.object_name}
    assert _logical_names(readable.eligible) == {legacy.object_name}


def test_weak_evidence_lists_decisions_without_a_bound_sidecar() -> None:
    first, second, third = _daily(1), _daily(2), _daily(3)
    stale = SidecarPair("999", _bound_pair(third).sidecar)
    plan = _plan(
        (first, second, third),
        retention=LogicalRetention(keep_dailies=1, legacy_tz=_TZ),
        sidecar_pairs=(_bound_pair(first), stale),
    )
    assert _logical_names(plan.eligible) == {first.object_name, second.object_name}
    attached = [item for item in plan.eligible if item.object.object_name == first.object_name]
    assert attached[0].sidecar == _bound_pair(first).sidecar
    assert plan.weak_evidence == tuple(sorted((second.object_name, third.object_name)))


def test_active_recovery_floor_pin_survives_the_window() -> None:
    pinned = _activation(1)
    plan = _plan(
        (_activation(1), _activation(2), _activation(3), _activation(4)),
        retention=LogicalRetention(
            legacy_tz=_TZ, active_pin_name=relative_name(pinned.object_name)
        ),
    )
    assert _logical_names(plan.eligible) == {_activation(2).object_name}
    assert _reason(plan, pinned) == "active recovery floor pin"


def _orphan(host: RetentionObject) -> OrphanSidecar:
    return OrphanSidecar(host, RetentionSidecar(f"{host.object_name}.ack.json", "orphan-pin", 7))


def test_logical_orphan_sidecar_needs_the_full_window_behind_it() -> None:
    gone_old, gone_inside = _daily(1), _daily(5)
    live = tuple(_daily(day) for day in range(2, 9))  # the full seven-member window

    plan = _plan(live, orphan_sidecars=(_orphan(gone_old), _orphan(gone_inside)))
    assert plan.blocked_reasons == ()
    assert [item.object_name for item in plan.orphan_sidecars] == [
        f"{gone_old.object_name}.ack.json"
    ]

    short = _plan(
        tuple(_daily(day) for day in range(3, 9)),  # one member short of the window
        orphan_sidecars=(_orphan(_daily(1)),),
    )
    assert short.orphan_sidecars == ()


def test_ambiguous_logical_orphan_observations_block() -> None:
    host = _daily(1)
    first = _orphan(host)
    second = OrphanSidecar(host, RetentionSidecar(f"{host.object_name}.ack.json", "other-pin", 8))
    plan = _plan(tuple(_daily(day) for day in range(2, 9)), orphan_sidecars=(first, second))
    assert "ambiguous orphan sidecar observation" in plan.blocked_reasons
    assert plan.orphan_sidecars == ()


def test_blocked_physical_plan_keeps_every_logical_decision() -> None:
    outside = (_daily(1), _daily(2), _daily(3))
    plan = _plan(
        outside,
        retention=LogicalRetention(keep_dailies=1, legacy_tz=_TZ),
        physical=_physical(unprotected_first=True),
    )
    assert "unprotected candidate exists" in plan.blocked_reasons
    assert plan.eligible == ()
    assert _logical_names(plan.retained) == {item.object_name for item in outside}
    assert plan.weak_evidence == tuple(sorted(item.object_name for item in outside))


def test_blocked_fold_keeps_bound_sidecars_and_no_weak_evidence() -> None:
    """Beyond-window objects with a verified binding stay non-weak through a blocked fold.

    Regression: the blocked branch folded logical eligibles into the physical
    bucket, so the physical sidecar index could not attach their (bound)
    sidecars and every beyond-window object turned into weak evidence.
    """
    outside = (_daily(1), _daily(2), _daily(3))
    pairs = tuple(_bound_pair(item) for item in outside)
    plan = _plan(
        outside,
        retention=LogicalRetention(keep_dailies=1, legacy_tz=_TZ),
        physical=_physical(unprotected_first=True),
        sidecar_pairs=pairs,
    )
    assert "unprotected candidate exists" in plan.blocked_reasons
    attached = {
        decision.object.object_name: decision.sidecar
        for decision in plan.retained
        if decision.object.kind == "logical"
    }
    assert set(attached) == {item.object_name for item in outside}
    for item in outside:
        assert attached[item.object_name] == _bound_pair(item).sidecar
    assert plan.weak_evidence == ()


def test_out_of_grammar_logical_objects_block_fail_closed() -> None:
    outside = _logical("ava-notes.txt")
    plan = _plan((_daily(1), outside))
    assert "logical object is outside its managed namespace" in plan.blocked_reasons
    assert plan.eligible == ()
    assert outside.object_name in _logical_names(plan.retained)

    duplicate = _plan((_daily(1), _daily(1)))
    assert "duplicate logical object pin token" in duplicate.blocked_reasons
    assert duplicate.eligible == ()


def test_plan_json_round_trip_carries_weak_evidence() -> None:
    plan = _plan((_daily(1), _daily(2)), sidecar_pairs=(_bound_pair(_daily(1)),))
    assert plan.weak_evidence == (_daily(2).object_name,)
    restored = RetentionPlan.from_json(plan.to_json())
    assert restored.weak_evidence == plan.weak_evidence

    raw = json.loads(plan.to_json())
    raw.pop("weak_evidence")
    legacy = RetentionPlan.from_json(json.dumps(raw))
    assert legacy.weak_evidence == ()


def test_logical_retention_depths_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        LogicalRetention(keep_dailies=0)
    with pytest.raises(ValueError, match="positive"):
        LogicalRetention(keep_pre_updates=0)


class _Reader:
    """A scripted inventory reader: one snapshot per call, the last sticking."""

    def __init__(self, *snapshots: InventorySnapshot) -> None:
        self._snapshots = list(snapshots)

    def snapshot(self) -> InventorySnapshot:
        return self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]


def test_build_local_evidence_snapshots_the_logical_namespace(tmp_path: Path) -> None:
    snapshot = InventorySnapshot((_daily(1),), ("ava-logical/junk.txt",))
    evidence = build_local_evidence(tmp_path, logical_reader=_Reader(snapshot))
    assert evidence.logical_inventory == (_daily(1),)
    assert "ava-logical/junk.txt" in evidence.malformed_names


def test_logical_snapshot_change_marks_the_evidence_malformed(tmp_path: Path) -> None:
    reader = _Reader(
        InventorySnapshot((_daily(1),), ()), InventorySnapshot((_daily(1), _daily(2)), ())
    )
    evidence = build_local_evidence(tmp_path, logical_reader=reader)
    assert "logical inventory changed during snapshot" in evidence.malformed_names


def test_write_dry_run_plan_requires_reader_and_policy_together(tmp_path: Path) -> None:
    reader: RetentionInventoryReader = _Reader(InventorySnapshot((), ()))
    with pytest.raises(ValueError, match="come together"):
        write_dry_run_plan(tmp_path, logical_reader=reader)
    with pytest.raises(ValueError, match="come together"):
        write_dry_run_plan(tmp_path, logical_retention=LogicalRetention())


def test_write_dry_run_plan_reports_logical_counts(tmp_path: Path) -> None:
    reader: RetentionInventoryReader = _Reader(InventorySnapshot((_daily(1), _daily(2)), ()))
    result = write_dry_run_plan(
        tmp_path, logical_reader=reader, logical_retention=LogicalRetention(legacy_tz=_TZ)
    )
    assert result.blocked  # the empty physical surface keeps the plan fail closed
    assert result.logical_object_count == 2
    assert result.logical_bytes == 20
    assert result.logical_eligible_objects == 0
    assert result.weak_evidence_objects == 2
