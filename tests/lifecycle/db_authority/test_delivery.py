"""Delivery of the active write generation, without a database.

The ledger side is settings-free: the pooler userlist, the launcher's class
grant and the operator's admitted-runtime consumption all read the ledger and
its digest-bound secret. The boot pass keeps a launcher delivery for this home
only, delivers an admitted operator process its gateway login, and records a
refusal that turns a dial of the credential-free endpoint into a named error.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from shared import dotenv_boot
from shared.cluster import authority
from shared.cluster.authority import delivery
from shared.db_connections import NoDatabaseAuthorityError, _guard_db_url

_ENDPOINT = "postgresql://ava@127.0.0.1:6433/ava"
_REPO = Path(__file__).resolve().parents[3]


def _encrypt(name: str, _password: str) -> str:
    return f"SCRAM-SHA-256$4096:c2VlZA==${name}"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = (tmp_path / "home").resolve()
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def seeded(home: Path, seed_write_generation: Callable[[Path], Any]) -> Any:
    secret = seed_write_generation(home)
    authority.ensure_pooler_admin(home, encrypt=_encrypt)
    return secret


def _intent(home: Path, checkout: Path) -> None:
    path = home / "start-intent.json"
    path.write_text(json.dumps({"home": str(home), "checkout": str(checkout)}))
    path.chmod(0o600)


# ── ledger-side delivery ─────────────────────────────────────────────────────


def test_userlist_holds_exactly_the_generation_and_the_admin_console(
    home: Path, seeded: Any
) -> None:
    body = authority.render_userlist(home, authority.active_generation(home)).decode()
    admin = authority.read_pooler_admin(home)
    assert body.splitlines() == [
        f'"{seeded.roles.gateway.name}" "{seeded.roles.gateway.verifier}"',
        f'"{seeded.roles.runner.name}" "{seeded.roles.runner.verifier}"',
        f'"{authority.POOLER_ADMIN}" "{admin.verifier}"',
    ]
    for password in (seeded.roles.gateway.password, seeded.roles.runner.password, admin.password):
        assert password not in body


def test_pooler_admin_is_created_once_and_never_rotated(home: Path, seeded: Any) -> None:
    del seeded
    first = authority.read_pooler_admin(home)
    again = authority.ensure_pooler_admin(home, encrypt=_encrypt)
    assert again == first
    info = delivery.pooler_admin_path(home).stat()
    assert info.st_mode & 0o077 == 0


def test_write_grant_is_the_active_class_login_bound_to_the_ledger(home: Path, seeded: Any) -> None:
    grant = authority.write_grant(home, "runner")
    assert (grant.role, grant.password) == (seeded.roles.runner.name, seeded.roles.runner.password)
    assert grant.reference == {
        "number": 0,
        "credential_digest": authority.active_generation(home).credential_digest,
    }
    assert grant.dsn(_ENDPOINT) == (
        f"postgresql://{grant.role}:{grant.password}@127.0.0.1:6433/ava"
    )


def test_a_tampered_secret_is_never_delivered(home: Path, seeded: Any) -> None:
    del seeded
    path = home / "db-authority" / "generations" / "0.json"
    body = json.loads(path.read_text())
    body["roles"]["gateway"]["password"] = "x" * 40
    path.write_text(json.dumps(body))
    with pytest.raises(authority.LedgerRefusedError, match="does not match the ledger"):
        authority.write_grant(home, "gateway")


def test_a_pending_generation_is_never_delivered(home: Path) -> None:
    from shared.cluster.authority.ledger import begin_mint

    birth = authority.BirthAuthority()
    groups = authority.Groups(gateway=authority.GATEWAY_GROUP, runner=authority.RUNNER_GROUP)
    authority.create_ledger(home, owner="ava", groups=groups, authority=birth)
    begin_mint(home, birth, encrypt=_encrypt)
    with pytest.raises(authority.LedgerRefusedError, match="no active generation"):
        authority.write_grant(home, "gateway")


# ── admitted runtime ─────────────────────────────────────────────────────────


def test_source_runtime_must_be_the_home_checkout(home: Path, tmp_path: Path) -> None:
    _intent(home, _REPO)
    delivery.require_admitted_runtime(home, code_root=_REPO, prefix=Path(sys.prefix))
    other = tmp_path / "other-checkout"
    other.mkdir()
    with pytest.raises(authority.AuthorityRefusedError, match="not the home's source checkout"):
        delivery.require_admitted_runtime(home, code_root=other, prefix=Path(sys.prefix))


def test_release_runtime_must_be_the_selected_image(home: Path) -> None:
    selected, stale = "a" * 64, "b" * 64
    releases = home / "releases"
    for digest in (selected, stale):
        (releases / digest / "venv" / "site-packages").mkdir(parents=True)
    (releases / "current-release").write_text(
        json.dumps({"artifact_digest": selected, "manifest_digest": "c" * 64})
    )
    image = releases / selected / "venv"
    delivery.require_admitted_runtime(home, code_root=image / "site-packages", prefix=image)
    old = releases / stale / "venv"
    with pytest.raises(authority.AuthorityRefusedError, match="selected release image"):
        delivery.require_admitted_runtime(home, code_root=old / "site-packages", prefix=old)
    # A source checkout never receives a release home's authority.
    with pytest.raises(authority.AuthorityRefusedError, match="selected release image"):
        delivery.require_admitted_runtime(home, code_root=_REPO, prefix=Path(sys.prefix))


# ── the boot pass ────────────────────────────────────────────────────────────


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch, home: Path) -> Iterator[Path]:
    """Point the boot pass at `home` as this process's anchored home.

    The authority pass force-assigns and DROPS cluster keys in os.environ
    directly; the whole environment is restored after each test so no later
    test inherits this fake unit's keys."""
    saved = dict(os.environ)
    env_file = home / ".env"
    env_file.write_text(f"AVA_DB_URL={_ENDPOINT}\n")
    monkeypatch.setattr(dotenv_boot, "_HOME", home)
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", home / "absent-mirror.env")
    monkeypatch.setattr(dotenv_boot, "_db_authority_refusal", None)
    for key in (
        "AVA_PROCESS_PROFILE",
        dotenv_boot.LAUNCHER_PROFILE_ENV_KEY,
        authority.GENERATION_ENV,
    ):
        os.environ.pop(key, None)
    os.environ["AVA_DB_URL"] = _ENDPOINT
    try:
        yield home
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_a_delivered_login_for_this_home_survives_the_env_file(
    boot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered = "postgresql://ava_g4_gateway:delivered@127.0.0.1:6433/ava"
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "gateway")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", delivered)
    monkeypatch.setitem(os.environ, authority.GENERATION_ENV, "4")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == delivered
    assert dotenv_boot.db_authority_refusal() is None


def test_a_sibling_homes_delivery_is_replaced_by_this_homes_endpoint(
    boot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "gateway")
    monkeypatch.setitem(
        os.environ, "AVA_DB_URL", "postgresql://ava_g4_gateway:sibling@127.0.0.1:7433/ava"
    )
    monkeypatch.setitem(os.environ, authority.GENERATION_ENV, "4")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT


def test_an_admitted_operator_process_receives_the_gateway_login(boot: Path, seeded: Any) -> None:
    _intent(boot, _REPO)
    dotenv_boot._enforce_cluster_env_authority()
    gateway = seeded.roles.gateway
    assert os.environ["AVA_DB_URL"] == (
        f"postgresql://{gateway.name}:{gateway.password}@127.0.0.1:6433/ava"
    )
    assert os.environ[authority.GENERATION_ENV] == "0"
    assert dotenv_boot.db_authority_refusal() is None


def test_a_foreign_runtime_gets_nothing_and_its_dial_fails_by_name(
    boot: Path, seeded: Any, tmp_path: Path
) -> None:
    del seeded
    _intent(boot, tmp_path / "another-checkout")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    assert authority.GENERATION_ENV not in os.environ
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "source checkout" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="credential-free"):
        _guard_db_url(_ENDPOINT)
    # A URL carrying a credential is not the undelivered endpoint.
    assert _guard_db_url("postgresql://ava_g0_gateway:pw@127.0.0.1:6433/ava")


def test_a_launched_process_without_delivery_is_refused(
    boot: Path, seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded
    _intent(boot, _REPO)
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "runner")
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "only the root launcher delivers" in refusal


def test_a_home_without_a_ledger_is_untouched(boot: Path) -> None:
    dotenv_boot._enforce_cluster_env_authority()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    assert dotenv_boot.db_authority_refusal() is None
    assert _guard_db_url(_ENDPOINT) == _ENDPOINT
