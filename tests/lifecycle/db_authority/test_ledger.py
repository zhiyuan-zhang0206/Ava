"""The private generation ledger without a database: transitions, refusals,
secret-file adoption, exclusive publication, digests and file custody."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.cluster.authority import ledger as store
from shared.cluster.authority.model import (
    BirthAuthority,
    ClosureEvidence,
    CutoverAuthority,
    Generation,
    Groups,
    Ledger,
    LedgerRefusedError,
    OperationAuthority,
    Origin,
    Revoked,
    VerifiedGeneration,
)

_GROUPS = Groups(gateway="ava_gateway", runner="ava_runner")


class _Encrypt:
    """A stand-in verifier function that counts how often a secret is generated."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, name: str, password: str) -> str:
        self.calls += 1
        return f"SCRAM-SHA-256$4096:c2FsdA==${name}:{len(password)}"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "home"
    path.mkdir()
    store.create_ledger(path, owner="ava", groups=_GROUPS, authority=BirthAuthority())
    return path


def _verified(generation: Generation) -> VerifiedGeneration:
    return VerifiedGeneration(generation.number, generation.credential_digest, generation.roles)


def _evidence(*roles: str) -> ClosureEvidence:
    return ClosureEvidence(roles=roles, terminated=0, rounds=0)


def _op() -> OperationAuthority:
    return OperationAuthority(operation=uuid4(), direction="candidate")


def _born(home: Path) -> Generation:
    pending = store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    return store.activate(home, BirthAuthority(), _verified(pending))


def _rotate(home: Path, authority: OperationAuthority) -> Generation:
    (revoking,) = store.begin_revoke(home, authority)
    store.mark_closed(home, authority, _evidence(*revoking.roles))
    pending = store.begin_mint(home, authority, encrypt=_Encrypt())
    return store.activate(home, authority, _verified(pending))


def test_generations_advance_monotonically_through_every_state(home: Path) -> None:
    g0 = _born(home)
    assert (g0.number, g0.roles, g0.origin) == (
        0,
        ("ava_g0_gateway", "ava_g0_runner"),
        Origin(kind="birth"),
    )
    first, second = _op(), _op()
    g1 = _rotate(home, first)
    g2 = _rotate(home, second)
    ledger = store.require_ledger(home)
    assert (g1.number, g2.number, ledger.counter) == (1, 2, 2)
    assert ledger.active == g2 and ledger.pending is None
    assert [(e.number, e.state) for e in ledger.revoked] == [(0, "closed"), (1, "closed")]
    assert g2.origin == Origin(
        kind="operation", operation=str(second.operation), direction="candidate"
    )
    assert sorted(p.name for p in (home / "db-authority" / "generations").iterdir()) == ["2.json"]


def test_exact_retry_returns_the_same_pending_generation(home: Path) -> None:
    encrypt = _Encrypt()
    first = store.begin_mint(home, BirthAuthority(), encrypt=encrypt)
    assert store.begin_mint(home, BirthAuthority(), encrypt=encrypt) == first
    assert encrypt.calls == 2  # one gateway and one runner verifier, generated once
    assert store.require_ledger(home).counter == 0


def test_secret_published_before_a_crash_is_adopted_not_regenerated(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_path: Path, _data: bytes) -> None:
        raise OSError("crash before pending")

    encrypt = _Encrypt()
    with monkeypatch.context() as patched:
        patched.setattr(store, "write_private_bytes", crash)
        with pytest.raises(OSError, match="crash before pending"):
            store.begin_mint(home, BirthAuthority(), encrypt=encrypt)
    published = (home / "db-authority" / "generations" / "0.json").read_bytes()
    assert store.require_ledger(home).counter is None
    pending = store.begin_mint(home, BirthAuthority(), encrypt=encrypt)
    assert encrypt.calls == 2
    assert pending.credential_digest == store.credential_digest(published)
    assert (home / "db-authority" / "generations" / "0.json").read_bytes() == published


def test_publication_never_overwrites_an_existing_secret(home: Path) -> None:
    target = home / "db-authority" / "generations" / "5.json"
    store._publish_exclusive(target, b"first")
    with pytest.raises(FileExistsError):
        store._publish_exclusive(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in target.parent.iterdir()] == ["5.json"]


def test_mint_refuses_while_a_generation_is_active_or_unclosed(home: Path) -> None:
    _born(home)
    authority = _op()
    for retry in (BirthAuthority(), authority):
        with pytest.raises(LedgerRefusedError, match="is active; revoke it first"):
            store.begin_mint(home, retry, encrypt=_Encrypt())
    store.begin_revoke(home, authority)
    with pytest.raises(LedgerRefusedError, match="not proven closed"):
        store.begin_mint(home, authority, encrypt=_Encrypt())


def test_birth_and_cutover_mint_only_generation_zero(home: Path) -> None:
    _born(home)
    _rotate(home, _op())
    for authority in (BirthAuthority(), CutoverAuthority()):
        with pytest.raises(LedgerRefusedError, match="mint only generation 0"):
            store.begin_mint(home, authority, encrypt=_Encrypt())


def test_operation_authority_cannot_birth_a_home(home: Path) -> None:
    with pytest.raises(LedgerRefusedError, match="after the home's first generation"):
        store.begin_mint(home, _op(), encrypt=_Encrypt())


def test_birth_and_cutover_do_not_adopt_each_other(home: Path) -> None:
    store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    with pytest.raises(LedgerRefusedError, match="generation 0"):
        store.begin_mint(home, CutoverAuthority(), encrypt=_Encrypt())


def test_a_foreign_pending_generation_is_neither_adopted_nor_activated(home: Path) -> None:
    _born(home)
    owner, other = _op(), _op()
    (revoking,) = store.begin_revoke(home, owner)
    store.mark_closed(home, owner, _evidence(*revoking.roles))
    pending = store.begin_mint(home, owner, encrypt=_Encrypt())
    with pytest.raises(LedgerRefusedError, match="has another origin"):
        store.begin_mint(home, other, encrypt=_Encrypt())
    with pytest.raises(LedgerRefusedError, match="exact pending generation"):
        store.activate(home, other, _verified(pending))


@pytest.mark.parametrize(
    "change",
    [
        {"number": 1},
        {"credential_digest": "0" * 64},
        {"roles": ("ava_g0_gateway", "ava_g9_runner")},
    ],
)
def test_activation_requires_the_exact_pending_pair(home: Path, change: dict[str, Any]) -> None:
    pending = store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    fields = {**_verified(pending).__dict__, **change}
    with pytest.raises(LedgerRefusedError, match="exact pending generation"):
        store.activate(home, BirthAuthority(), VerifiedGeneration(**fields))
    assert store.require_ledger(home).active is None


def test_activation_is_idempotent_for_the_same_generation(home: Path) -> None:
    pending = store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    first = store.activate(home, BirthAuthority(), _verified(pending))
    assert store.activate(home, BirthAuthority(), _verified(pending)) == first


def test_closure_evidence_must_cover_both_logins(home: Path) -> None:
    _born(home)
    authority = _op()
    store.begin_revoke(home, authority)
    with pytest.raises(LedgerRefusedError, match=r"does not cover \['ava_g0_runner'\]"):
        store.mark_closed(home, authority, _evidence("ava_g0_gateway"))
    assert store.require_ledger(home).revoked[0].state == "revoking"
    assert (home / "db-authority" / "generations" / "0.json").exists()


def test_revoke_is_idempotent_and_irreversible(home: Path) -> None:
    _born(home)
    authority = _op()
    first = store.begin_revoke(home, authority)
    assert store.begin_revoke(home, authority) == first
    ledger = store.require_ledger(home)
    assert ledger.active is None and ledger.counter == 0
    store.mark_closed(home, authority, _evidence(*first[0].roles))
    pending = store.begin_mint(home, authority, encrypt=_Encrypt())
    assert pending.number == 1 and pending.roles == ("ava_g1_gateway", "ava_g1_runner")


def test_drop_outcomes_are_recorded_only_for_closed_generations(home: Path) -> None:
    _born(home)
    authority = _op()
    (revoking,) = store.begin_revoke(home, authority)
    with pytest.raises(LedgerRefusedError, match="not a closed revoked generation"):
        store.record_drops(home, authority, {0: None})
    store.mark_closed(home, authority, _evidence(*revoking.roles))
    ledger = store.record_drops(home, authority, {0: "ava_g0_runner: still referenced"})
    assert (ledger.revoked[0].dropped, ledger.revoked[0].drop_error) == (
        False,
        "ava_g0_runner: still referenced",
    )
    ledger = store.record_drops(home, authority, {0: None})
    assert (ledger.revoked[0].dropped, ledger.revoked[0].drop_error) == (True, None)


def test_the_secret_is_bound_to_the_ledger_digest(home: Path) -> None:
    generation = _born(home)
    secret = store.read_secret(home, generation)
    assert (secret.number, secret.roles.gateway.name) == (0, "ava_g0_gateway")
    assert len(secret.roles.runner.password) >= 32
    path = home / "db-authority" / "generations" / "0.json"
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b'"number": 0', b'"number": 0 '))
    with pytest.raises(LedgerRefusedError, match="does not match the ledger"):
        store.read_secret(home, generation)


def test_store_files_are_owner_only(home: Path) -> None:
    _born(home)
    root = home / "db-authority"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "generations").stat().st_mode & 0o777 == 0o700
    for path in (root / "ledger.json", root / "generations" / "0.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def _corrupt_json(home: Path) -> None:
    (home / "db-authority" / "ledger.json").write_text("{not json")


def _extra_key(home: Path) -> None:
    path = home / "db-authority" / "ledger.json"
    body = json.loads(path.read_text())
    path.write_text(json.dumps({**body, "unexpected": True}))


def _other_home(home: Path) -> None:
    path = home / "db-authority" / "ledger.json"
    body = json.loads(path.read_text())
    path.write_text(json.dumps({**body, "home": "/elsewhere"}))


def _group_readable(home: Path) -> None:
    (home / "db-authority" / "ledger.json").chmod(0o640)


def _open_directory(home: Path) -> None:
    (home / "db-authority").chmod(0o755)


def _symlinked(home: Path) -> None:
    path = home / "db-authority" / "ledger.json"
    target = home / "elsewhere.json"
    path.rename(target)
    path.symlink_to(target)


def _orphan_secrets(home: Path) -> None:
    (home / "db-authority" / "ledger.json").unlink()


def _stray_secret(home: Path) -> None:
    (home / "db-authority" / "generations" / "7.json").write_bytes(b"{}")
    (home / "db-authority" / "generations" / "7.json").chmod(0o600)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_corrupt_json, "corrupt"),
        (_extra_key, "corrupt"),
        (_other_home, "another home"),
        (_group_readable, "not owner-only"),
        (_open_directory, "not owner-only"),
        (_symlinked, "not a real private node"),
        (_orphan_secrets, "secrets exist without a ledger"),
    ],
)
def test_a_damaged_store_refuses(home: Path, damage: Any, reason: str) -> None:
    store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    damage(home)
    with pytest.raises(LedgerRefusedError, match=reason):
        store.load_ledger(home)


def test_an_unexplained_secret_refuses_the_next_mint(home: Path) -> None:
    _born(home)
    authority = _op()
    (revoking,) = store.begin_revoke(home, authority)
    store.mark_closed(home, authority, _evidence(*revoking.roles))
    _stray_secret(home)
    with pytest.raises(LedgerRefusedError, match=r"unexplained generation secrets: \[7\]"):
        store.begin_mint(home, authority, encrypt=_Encrypt())


def test_create_ledger_keeps_an_identical_ledger_and_refuses_another(home: Path) -> None:
    before = (home / "db-authority" / "ledger.json").read_bytes()
    store.create_ledger(home, owner="ava", groups=_GROUPS, authority=CutoverAuthority())
    assert (home / "db-authority" / "ledger.json").read_bytes() == before
    with pytest.raises(LedgerRefusedError, match="another owner"):
        store.create_ledger(home, owner="ava_main", groups=_GROUPS, authority=CutoverAuthority())


def test_an_absent_store_reads_as_no_ledger(tmp_path: Path) -> None:
    assert store.load_ledger(tmp_path.resolve()) is None
    with pytest.raises(LedgerRefusedError, match="no database authority ledger"):
        store.require_ledger(tmp_path.resolve())


_DIGEST = "a" * 64
_BIRTH = Origin(kind="birth")


def _generation(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "gateway": f"ava_g{number}_gateway",
        "runner": f"ava_g{number}_runner",
        "credential_digest": _DIGEST,
        "origin": _BIRTH,
    }


def _revoked(number: int) -> Revoked:
    return Revoked(
        number=number,
        gateway=f"ava_g{number}_gateway",
        runner=f"ava_g{number}_runner",
        state="closed",
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"counter": 1, "active": Generation(**_generation(0))},  # number 1 unaccounted
        {"counter": 1, "active": Generation(**_generation(1))},  # number 0 unaccounted
        {"counter": 1, "pending": Generation(**_generation(0)), "revoked": (_revoked(1),)},
        {
            "counter": 0,
            "active": Generation(**_generation(0)),
            "pending": Generation(**_generation(0)),
        },
        {"counter": 1, "revoked": (_revoked(1), _revoked(0))},  # unordered
        {"counter": None, "active": Generation(**_generation(0))},
        {
            "counter": 0,
            "active": Generation.model_validate({**_generation(0), "gateway": "ava_gateway"}),
        },
        {"owner": "ava_runner"},
    ],
)
def test_the_ledger_model_rejects_incoherent_records(fields: dict[str, Any]) -> None:
    base: dict[str, Any] = {"version": 1, "home": "/h", "owner": "ava", "groups": _GROUPS}
    with pytest.raises(ValidationError):
        Ledger(**{**base, **fields})


def test_a_loosened_store_is_refused_not_repaired_by_a_mutation(home: Path) -> None:
    _open_directory(home)
    with pytest.raises(LedgerRefusedError, match="not owner-only"):
        store.begin_mint(home, BirthAuthority(), encrypt=_Encrypt())
    assert (home / "db-authority").stat().st_mode & 0o777 == 0o755
