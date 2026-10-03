"""The private generation ledger without a database: transitions, refusals,
secret-file adoption, exclusive publication, digests and file custody."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from base.cluster.authority import ledger as store
from base.cluster.authority.model import (
    Generation,
    Groups,
    Ledger,
    LedgerRefusedError,
    Origin,
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
    store.create_ledger(path, owner="ava", groups=_GROUPS)
    return path


def _verified(generation: Generation) -> VerifiedGeneration:
    return VerifiedGeneration(generation.number, generation.credential_digest, generation.roles)


def _born(home: Path) -> Generation:
    pending = store.begin_mint(home, encrypt=_Encrypt())
    return store.activate(home, _verified(pending))


def test_birth_records_generation_zero_pending_then_active(home: Path) -> None:
    pending = store.begin_mint(home, encrypt=_Encrypt())
    ledger = store.require_ledger(home)
    assert ledger.pending == pending and ledger.active is None
    active = store.activate(home, _verified(pending))
    assert (active.number, active.roles, active.origin) == (
        0,
        ("ava_g0_gateway", "ava_g0_runner"),
        Origin(kind="birth"),
    )
    ledger = store.require_ledger(home)
    assert ledger.active == active and ledger.pending is None
    assert sorted(p.name for p in (home / "db-authority" / "generations").iterdir()) == ["0.json"]


def test_exact_retry_returns_the_same_pending_generation(home: Path) -> None:
    encrypt = _Encrypt()
    first = store.begin_mint(home, encrypt=encrypt)
    assert store.begin_mint(home, encrypt=encrypt) == first
    assert encrypt.calls == 2  # one gateway and one runner verifier, generated once


def test_secret_published_before_a_crash_is_adopted_not_regenerated(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_path: Path, _data: bytes) -> None:
        raise OSError("crash before pending")

    encrypt = _Encrypt()
    with monkeypatch.context() as patched:
        patched.setattr(store, "write_private_bytes", crash)
        with pytest.raises(OSError, match="crash before pending"):
            store.begin_mint(home, encrypt=encrypt)
    published = (home / "db-authority" / "generations" / "0.json").read_bytes()
    assert store.require_ledger(home).generation is None
    pending = store.begin_mint(home, encrypt=encrypt)
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


def test_mint_refuses_once_the_generation_is_active(home: Path) -> None:
    _born(home)
    with pytest.raises(LedgerRefusedError, match="already active"):
        store.begin_mint(home, encrypt=_Encrypt())


def test_a_ledger_written_before_generations_stopped_rotating_loads(home: Path) -> None:
    """Production's ledger records `counter: 0`, `revoked: []` and an origin with
    null `operation`/`direction`; it loads unchanged and is rewritten without them."""
    _born(home)
    path = home / "db-authority" / "ledger.json"
    recorded = json.loads(path.read_text())
    recorded["counter"] = 0
    recorded["revoked"] = []
    recorded["active"]["origin"] = {"direction": None, "kind": "cutover", "operation": None}
    path.write_text(json.dumps(recorded))
    ledger = store.require_ledger(home)
    assert ledger.active is not None and ledger.active.origin == Origin(kind="cutover")
    store._write(home, ledger)
    rewritten = json.loads(path.read_text())
    assert "counter" not in rewritten and "revoked" not in rewritten
    assert rewritten["active"]["origin"] == {"kind": "cutover"}


@pytest.mark.parametrize(
    "legacy",
    [
        {"counter": 1},
        {"counter": 1, "revoked": []},
        {"revoked": [{"number": 0}]},
    ],
)
def test_a_ledger_that_recorded_a_rotation_is_refused(home: Path, legacy: dict[str, Any]) -> None:
    _born(home)
    path = home / "db-authority" / "ledger.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **legacy}))
    with pytest.raises(LedgerRefusedError, match="corrupt"):
        store.load_ledger(home)


@pytest.mark.parametrize(
    "change",
    [
        {"number": 1},
        {"credential_digest": "0" * 64},
        {"roles": ("ava_g0_gateway", "ava_g9_runner")},
    ],
)
def test_activation_requires_the_exact_pending_pair(home: Path, change: dict[str, Any]) -> None:
    pending = store.begin_mint(home, encrypt=_Encrypt())
    fields = {**_verified(pending).__dict__, **change}
    with pytest.raises(LedgerRefusedError, match="exact pending generation"):
        store.activate(home, VerifiedGeneration(**fields))
    assert store.require_ledger(home).active is None


def test_activation_is_idempotent_for_the_same_generation(home: Path) -> None:
    pending = store.begin_mint(home, encrypt=_Encrypt())
    first = store.activate(home, _verified(pending))
    assert store.activate(home, _verified(pending)) == first


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
    stray = home / "db-authority" / "generations" / "7.json"
    stray.parent.mkdir(mode=0o700, exist_ok=True)
    stray.write_bytes(b"{}")
    stray.chmod(0o600)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_corrupt_json, "corrupt"),
        (_extra_key, "corrupt"),
        (_other_home, "another home"),
        (_group_readable, "not owner-only"),
        (_open_directory, "not owner-only"),
        (_symlinked, "not a real private node"),
        (_orphan_secrets, "secret exists without a ledger"),
    ],
)
def test_a_damaged_store_refuses(home: Path, damage: Any, reason: str) -> None:
    store.begin_mint(home, encrypt=_Encrypt())
    damage(home)
    with pytest.raises(LedgerRefusedError, match=reason):
        store.load_ledger(home)


def test_an_unexplained_secret_refuses_the_mint(home: Path) -> None:
    _stray_secret(home)
    with pytest.raises(LedgerRefusedError, match="unknown file in the database authority store"):
        store.begin_mint(home, encrypt=_Encrypt())


def test_create_ledger_keeps_an_identical_ledger_and_refuses_another(home: Path) -> None:
    before = (home / "db-authority" / "ledger.json").read_bytes()
    store.create_ledger(home, owner="ava", groups=_GROUPS)
    assert (home / "db-authority" / "ledger.json").read_bytes() == before
    with pytest.raises(LedgerRefusedError, match="another owner"):
        store.create_ledger(home, owner="ava_main", groups=_GROUPS)


def test_an_absent_store_reads_as_no_ledger(tmp_path: Path) -> None:
    assert store.load_ledger(tmp_path.resolve()) is None
    with pytest.raises(LedgerRefusedError, match="no database authority ledger"):
        store.require_ledger(tmp_path.resolve())


_DIGEST = "a" * 64
_BIRTH = Origin(kind="birth")


def _generation(**changes: Any) -> Generation:
    fields: dict[str, Any] = {
        "number": 0,
        "gateway": "ava_g0_gateway",
        "runner": "ava_g0_runner",
        "credential_digest": _DIGEST,
        "origin": _BIRTH,
    }
    return Generation.model_validate({**fields, **changes})


@pytest.mark.parametrize(
    "fields",
    [
        {"active": _generation(), "pending": _generation()},
        {"counter": 0},  # a recorded counter names a generation the ledger does not hold
        {"active": _generation(gateway="ava_gateway")},  # a login named like a group
        {"active": _generation(runner="ava_g0_gateway")},  # one name twice
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
        store.begin_mint(home, encrypt=_Encrypt())
    assert (home / "db-authority").stat().st_mode & 0o777 == 0o755
