# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Sidecar pair and orphan rules (P1b, design section 2.5): the COS and
Baidu identity-bound delete adapters, the OSS/Baidu inventory capture of
pairs and orphans, and the policy classification. The executor-side chain
lives in ``test_pitr_retention_delete``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from services.pitr.baidu_inventory import BaiduRetentionInventoryReader
from services.pitr.baidu_store import BaiduRetentionDeleteStore
from services.pitr.base_manifest import CandidateManifest
from services.pitr.checksums import MD5
from services.pitr.cos_store import COSRetentionDeleteStore
from services.pitr.object_store import PermanentObjectStoreError, TransientObjectStoreError
from services.pitr.retention_delete import DeleteOutcome
from services.pitr.retention_manifest import (
    OrphanSidecar,
    RetentionObject,
    RetentionSidecar,
    SidecarPair,
)
from services.pitr.retention_policy import RetentionEvidence, plan_retention
from tests.services.baidu_test_support import (
    APP_ROOT,
    FakePcs,
    FakeTokenManager,
    make_store,
    pcs_client_for,
)
from tests.services.cos_test_support import FakeCos, cos_client_for
from tests.services.oss_test_support import PREFIX, FakeOssBucket, make_inventory
from tests.services.test_pitr_retention_policy import (
    SEGMENT,
    _candidate,
    _evidence,
    _inventory,
    _proof,
    _remote_wal,
)

_CHAIN_A = "20260801T000001Z"
_CHAIN_B = "20260802T000002Z"
_CHAIN_C = "20260803T000003Z"


# ── COS identity-bound delete ──


def test_cos_delete_if_match_requires_the_live_etag() -> None:
    fake = FakeCos()
    name = "ava-pitr-cutover/wal/000000010000000000000001.enc"
    etag = fake.seed(name, b"ciphertext")
    store = COSRetentionDeleteStore.from_client(cos_client_for(fake))

    assert store.delete_if_match(name, etag) is DeleteOutcome.DELETED
    assert fake.deleted == [name]
    assert name not in fake.objects

    assert store.delete_if_match(name, etag) is DeleteOutcome.ABSENT


def test_cos_delete_if_match_refuses_mismatch_and_maps_failures() -> None:
    fake = FakeCos()
    name = "ava-pitr-cutover/wal/000000010000000000000002.enc"
    fake.seed(name, b"ciphertext")
    store = COSRetentionDeleteStore.from_client(cos_client_for(fake))

    assert store.delete_if_match(name, "0" * 32) is DeleteOutcome.MISMATCH
    assert name in fake.objects

    fake.delete_error = 503
    with pytest.raises(TransientObjectStoreError):
        store.delete_if_match(name, hashlib.md5(b"ciphertext").hexdigest())  # noqa: S324 — fixture digest

    fake.delete_error = 403
    with pytest.raises(PermanentObjectStoreError):
        store.delete_if_match(name, hashlib.md5(b"ciphertext").hexdigest())  # noqa: S324 — fixture digest


# ── Baidu identity-bound delete ──


def test_baidu_delete_if_match_requires_the_live_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    target = "ava-pitr/wal/00000001/000000010000000000000001.enc"
    row = fake.seed_file(f"{APP_ROOT}/{target}", size=10, md5="server-digest")
    store = BaiduRetentionDeleteStore.from_store(make_store(fake, monkeypatch))
    identity = f"{row['fs_id']}:server-digest"

    assert store.delete_if_match(target, identity) is DeleteOutcome.DELETED
    assert fake.deleted == [f"{APP_ROOT}/{target}"]
    assert f"{APP_ROOT}/{target}" not in fake.files

    assert store.delete_if_match(target, identity) is DeleteOutcome.ABSENT


def test_baidu_delete_if_match_refuses_mismatch_and_maps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePcs()
    target = "ava-pitr/wal/00000001/000000010000000000000002.enc"
    row = fake.seed_file(f"{APP_ROOT}/{target}", size=10, md5="server-digest")
    store = BaiduRetentionDeleteStore.from_store(make_store(fake, monkeypatch))
    identity = f"{row['fs_id']}:server-digest"

    assert store.delete_if_match(target, "999:server-digest") is DeleteOutcome.MISMATCH
    assert f"{APP_ROOT}/{target}" in fake.files

    fake.delete_errno = 31198  # called too frequently
    with pytest.raises(TransientObjectStoreError):
        store.delete_if_match(target, identity)

    fake.delete_errno = 31064  # access denied
    with pytest.raises(PermanentObjectStoreError):
        store.delete_if_match(target, identity)


# ── OSS inventory capture ──


def _oss_sidecar_bytes(host_name: str, host_payload: bytes, host_etag: str) -> bytes:
    return json.dumps(
        {
            "object_name": host_name,
            "pin_token": host_etag,
            "size": len(host_payload),
            "checksum_algo": MD5,
            "checksum_value": hashlib.md5(host_payload).hexdigest(),  # noqa: S324 — fixture digest
            "metadata": {},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_oss_inventory_captures_pairs_and_orphans() -> None:
    fake = FakeOssBucket()
    base_name = f"{PREFIX}/base/20260101T000000Z/" + "a" * 64 + "/base.tar.zst.enc"
    base_payload = b"base-ciphertext"
    base_etag = fake.seed(base_name, data=base_payload)
    sidecar_etag = fake.seed(
        f"{base_name}.ack.json", data=_oss_sidecar_bytes(base_name, base_payload, base_etag)
    )

    orphan_name = f"{PREFIX}/base/20251201T000000Z/" + "c" * 64 + "/base.tar.zst.enc"
    orphan_payload = b"gone-host"
    orphan_bytes = json.dumps(
        {
            "object_name": orphan_name,
            "pin_token": "opaque:pin",
            "size": 7,
            "checksum_algo": MD5,
            "checksum_value": "d" * 32,
            "metadata": {},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    orphan_etag = fake.seed(f"{orphan_name}.ack.json", data=orphan_bytes)

    # A sidecar outside the managed base namespace stays invisible (foreign).
    fake.seed(f"{PREFIX}/junk/foreign.ack.json", data=b"{}")

    snapshot = make_inventory(fake).snapshot()

    assert [item.object_name for item in snapshot.objects] == [base_name]
    assert snapshot.unknown_names == ()
    assert [pair.sidecar.object_name for pair in snapshot.sidecar_pairs] == [
        f"{base_name}.ack.json"
    ]
    pair = snapshot.sidecar_pairs[0]
    assert pair.host_pin_token == base_etag
    assert pair.sidecar.pin_token == sidecar_etag
    assert pair.sidecar.size == len(_oss_sidecar_bytes(base_name, base_payload, base_etag))

    assert [item.sidecar.object_name for item in snapshot.orphan_sidecars] == [
        f"{orphan_name}.ack.json"
    ]
    orphan = snapshot.orphan_sidecars[0]
    assert orphan.host.object_name == orphan_name
    assert orphan.host.kind == "base"
    assert orphan.host.pin_token == "opaque:pin"  # noqa: S105 — fixture identity
    assert orphan.sidecar.pin_token == orphan_etag
    assert orphan_payload  # host payload never re-read for orphans


# ── Baidu inventory capture ──


def test_baidu_inventory_captures_pairs_and_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePcs()
    reader = BaiduRetentionInventoryReader(
        app_root=APP_ROOT, prefix="ava-pitr", token_manager=FakeTokenManager()
    )
    monkeypatch.setattr(reader._store, "_client", lambda: pcs_client_for(fake))

    archive = "000000010000000000000001"
    rel = f"ava-pitr/wal/{archive[:8]}/{archive}.enc"
    wal_sidecar = {
        "object_name": rel,
        "pin_token": "5:m",
        "size": 10,
        "checksum_algo": MD5,
        "checksum_value": "m",
        "metadata": {"ava-archive-name": archive},
    }
    wal_data = json.dumps(wal_sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(f"{APP_ROOT}/{rel}", size=10, md5="m")
    wal_row = fake.seed_file(
        f"{APP_ROOT}/{rel}.ack.json",
        size=len(wal_data),
        md5="side-digest",
        dlink="https://dl.test/wal",
    )

    orphan_rel = "ava-pitr/base/20260101T000000Z/" + "d" * 64 + "/base.tar.zst.enc"
    orphan_sidecar = {
        "object_name": orphan_rel,
        "pin_token": "9:opaque",
        "size": 11,
        "checksum_algo": MD5,
        "checksum_value": "c",
        "metadata": {},
    }
    orphan_data = json.dumps(orphan_sidecar, sort_keys=True, separators=(",", ":")).encode()
    orphan_row = fake.seed_file(
        f"{APP_ROOT}/{orphan_rel}.ack.json",
        size=len(orphan_data),
        md5="orphan-digest",
        dlink="https://dl.test/orphan",
    )

    bodies = {"https://dl.test/wal": wal_data, "https://dl.test/orphan": orphan_data}

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=bodies[url.split("&", 1)[0]])

    monkeypatch.setattr(httpx, "get", fake_get)

    snapshot = reader.snapshot()

    assert snapshot.unknown_names == ()
    assert [item.object_name for item in snapshot.objects] == [rel]
    assert [pair.sidecar.object_name for pair in snapshot.sidecar_pairs] == [f"{rel}.ack.json"]
    pair = snapshot.sidecar_pairs[0]
    assert pair.host_pin_token == "5:m"  # noqa: S105 — fixture identity
    assert pair.sidecar.pin_token == f"{wal_row['fs_id']}:side-digest"

    assert [item.sidecar.object_name for item in snapshot.orphan_sidecars] == [
        f"{orphan_rel}.ack.json"
    ]
    orphan = snapshot.orphan_sidecars[0]
    assert orphan.host.object_name == orphan_rel
    assert orphan.host.kind == "base"
    assert orphan.host.pin_token == "9:opaque"  # noqa: S105 — fixture identity
    assert orphan.sidecar.pin_token == f"{orphan_row['fs_id']}:orphan-digest"


# ── policy classification ──


def _three_candidates() -> tuple[CandidateManifest, ...]:
    return (
        _candidate(_CHAIN_A, SEGMENT, 2 * SEGMENT),
        _candidate(_CHAIN_B, 2 * SEGMENT, 3 * SEGMENT),
        _candidate(_CHAIN_C, 3 * SEGMENT, 4 * SEGMENT),
    )


def _three_chain_evidence(
    candidates: tuple[CandidateManifest, ...],
    *,
    sidecar_pairs: tuple[SidecarPair, ...] = (),
    orphan_sidecars: tuple[OrphanSidecar, ...] = (),
    malformed: tuple[str, ...] = (),
) -> RetentionEvidence:
    proofs = tuple(_proof(item) for item in candidates)
    evidence = _evidence(candidates, proofs, _inventory(*candidates), malformed)
    return replace(evidence, sidecar_pairs=sidecar_pairs, orphan_sidecars=orphan_sidecars)


def test_plan_attaches_bound_sidecars_to_their_host_decisions() -> None:
    candidates = _three_candidates()
    first, second = candidates[0], candidates[1]
    first_base = f"{first.base_object.object_name}.ack.json"
    bound = SidecarPair(first.base_object.pin_token, RetentionSidecar(first_base, "side-pin", 5))
    plan = plan_retention(_three_chain_evidence(candidates, sidecar_pairs=(bound,)))
    attached = [d for d in plan.eligible if d.object.object_name == first.base_object.object_name]
    assert len(attached) == 1
    assert attached[0].sidecar == bound.sidecar

    retained_pair = SidecarPair(
        second.base_object.pin_token,
        RetentionSidecar(f"{second.base_object.object_name}.ack.json", "kept-pin", 5),
    )
    retained_plan = plan_retention(
        _three_chain_evidence(candidates, sidecar_pairs=(retained_pair,))
    )
    kept = [
        d for d in retained_plan.retained if d.object.object_name == second.base_object.object_name
    ]
    assert len(kept) == 1
    assert kept[0].sidecar == retained_pair.sidecar

    unbound = SidecarPair("wrong-pin", bound.sidecar)
    unbound_plan = plan_retention(_three_chain_evidence(candidates, sidecar_pairs=(unbound,)))
    assert all(d.sidecar is None for d in unbound_plan.eligible)
    assert all(d.sidecar is None for d in unbound_plan.retained)


def test_plan_keeps_only_orphans_inside_the_eligible_range() -> None:
    candidates = _three_candidates()
    first, second = candidates[0], candidates[1]
    first_base = RetentionObject(
        first.base_object.object_name,
        first.base_object.pin_token,
        first.base_object.ciphertext_size,
        None,
        "base",
        first.base_object.ciphertext_checksum_algo,
        first.base_object.ciphertext_checksum_value,
        (),
    )
    retained_base = RetentionObject(
        second.base_object.object_name,
        second.base_object.pin_token,
        second.base_object.ciphertext_size,
        None,
        "base",
        second.base_object.ciphertext_checksum_algo,
        second.base_object.ciphertext_checksum_value,
        (),
    )
    unknown_base = RetentionObject(
        f"pitr/base/20990101T000000Z/{'f' * 64}/base.tar.zst.enc",
        "x",
        5,
        None,
        "base",
        MD5,
        "e" * 32,
        (),
    )

    def orphan(host: RetentionObject, pin: str) -> OrphanSidecar:
        return OrphanSidecar(host, RetentionSidecar(f"{host.object_name}.ack.json", pin, 3))

    observations = (
        orphan(first_base, "orphan-01"),
        orphan(retained_base, "orphan-02"),
        orphan(unknown_base, "orphan-99"),
        orphan(_remote_wal(1), "orphan-wal-old"),
        orphan(_remote_wal(4), "orphan-wal-late"),
    )
    evidence = _three_chain_evidence(candidates, orphan_sidecars=observations)
    plan = plan_retention(evidence)

    assert plan.blocked_reasons == ()
    assert {item.object_name for item in plan.orphan_sidecars} == {
        f"{first_base.object_name}.ack.json",
        f"{_remote_wal(1).object_name}.ack.json",
    }
    round_tripped = type(plan).from_json(plan.to_json())
    assert round_tripped.orphan_sidecars == plan.orphan_sidecars


def test_conflicting_observations_block_and_zero_the_orphans() -> None:
    candidates = _three_candidates()
    first = candidates[0]
    sidecar = RetentionSidecar(f"{first.base_object.object_name}.ack.json", "one", 3)
    clash = RetentionSidecar(f"{first.base_object.object_name}.ack.json", "two", 3)
    first_base = RetentionObject(
        first.base_object.object_name,
        first.base_object.pin_token,
        first.base_object.ciphertext_size,
        None,
        "base",
        first.base_object.ciphertext_checksum_algo,
        first.base_object.ciphertext_checksum_value,
        (),
    )
    evidence = _three_chain_evidence(
        candidates,
        sidecar_pairs=(
            SidecarPair(first.base_object.pin_token, sidecar),
            SidecarPair(first.base_object.pin_token, clash),
        ),
        orphan_sidecars=(OrphanSidecar(first_base, sidecar),),
    )
    plan = plan_retention(evidence)

    assert "ambiguous sidecar observation" in plan.blocked_reasons
    assert plan.orphan_sidecars == ()
    assert plan.eligible == ()

    malformed_plan = plan_retention(
        _three_chain_evidence(
            candidates,
            orphan_sidecars=(OrphanSidecar(first_base, sidecar),),
            malformed=("broken/name",),
        )
    )
    assert malformed_plan.blocked_reasons
    assert malformed_plan.orphan_sidecars == ()
