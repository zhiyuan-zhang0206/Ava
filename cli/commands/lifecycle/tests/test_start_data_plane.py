"""`ava start`'s gateway data-plane dispatch brings up this cluster's own instance.

Every cluster owns its Postgres+Redis; `_ensure_gateway_data_plane` reads the
cluster's registry record and starts the per-cluster instance on the record's
pg/redis ports. A missing record (defensive) is a hard error, not a silent
fall-through onto some shared instance.
"""

from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest

from base import cluster
from base.config import settings
from base.db.tests.fakes import patch_database
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.lifecycle import start as _start

_PORTS = cast("cluster.ClusterPorts", {"gateway": 23000, "postgres": 23011, "redis": 23012})


def _rec() -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=_PORTS,
        gateway_home=str(cluster.default_home()),
        created_at="x",
    )


def test_gateway_data_plane_brings_up_own_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A born cluster → the per-cluster instance on the record's exact pg/redis
    ports, with each data-plane identity read from its own URL."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    # An established home: its database authority ledger exists.
    monkeypatch.setattr("base.cluster.authority.load_ledger", lambda _home: object())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "bearer")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "redis-admin")
    monkeypatch.setattr(cluster, "redis_password_from_env", lambda: "redis-runtime")
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:sek@127.0.0.1:23011/ava_main"
    )
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava:sek@127.0.0.1:23012/0")
    own_calls: list[dict[str, object]] = []
    monkeypatch.setattr(_ci, "ensure_cluster_storage", lambda **kw: own_calls.append(kw) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _start._ensure_gateway_data_plane() == 0
    # The root that holds the secret published the telemetry token the station probe reads.
    from base.cluster.authority.api import read_telemetry_token, telemetry_token
    from base.paths import ava_home

    assert read_telemetry_token(ava_home().resolve()) == telemetry_token("bearer")
    # The identity comes from the db_url username, not any name derivation.
    assert own_calls == [
        {
            "pg_port": 23011,
            "redis_port": 23012,
            "cluster_secret": "bearer",
            "redis_admin_password": "redis-admin",
            "redis_password": "redis-runtime",
            "redis_user": "ava",
        }
    ]


def test_gateway_data_plane_refuses_a_home_without_a_ledger_before_any_effect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A home born before the always-authenticated data plane (no ledger, not a
    first start in progress) is refused before any native effect — never
    converted by an ordinary start."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.cluster.authority.load_ledger", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.start_identity.needs_provision", lambda _home: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_storage",
        lambda **_kw: pytest.fail("native effect on a legacy home"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _start._ensure_gateway_data_plane() == 1
    assert "no conversion exists" in capsys.readouterr().err


def test_gateway_data_plane_no_record_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A not-yet-registered home (defensive) → a hard error, never a bring-up."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_storage",
        lambda **_kw: pytest.fail("bring-up without a record"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _start._ensure_gateway_data_plane() == 1


class _Ledger:
    def __init__(self, *, active: bool) -> None:
        self.owner = "ava"
        self.groups = "groups"
        self.active = SimpleNamespace(number=0) if active else None
        self.generation = SimpleNamespace(number=0)


class _Plane:
    """Recorded effects of `complete_gateway_data_plane` and the fake start state."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = {"active": True, "birth": False}


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> _Plane:
    """Every effect of `complete_gateway_data_plane`, recorded in order."""
    from base.cluster import authority
    from cli.commands.converge import health_preflight
    from cli.commands.data_plane import bringup

    recorded = _Plane()
    calls, state = recorded.calls, recorded.state

    def record(name: str, result: object = None) -> object:
        def effect(*_a: object, **_kw: object) -> object:
            calls.append(name)
            return result

        return effect

    @contextmanager
    def admin(*_a: object) -> Generator[str]:
        yield "admin-connection"

    monkeypatch.setattr(bringup, "prepare_memory_vectors", record("memory-vectors"))
    monkeypatch.setattr(bringup, "admin_session", admin)
    monkeypatch.setattr(bringup, "db_endpoint", lambda: "postgresql://ava@127.0.0.1:6433/ava")
    monkeypatch.setattr(bringup, "_ensure_pooler", record("pooler"))
    monkeypatch.setattr(bringup, "prove_generation_logins", record("prove-logins"))
    monkeypatch.setattr(bringup, "adopt_gateway_login", record("adopt"))
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava@127.0.0.1:6433/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://127.0.0.1:6380/0")
    monkeypatch.setattr(cluster, "ensure_pgvector_extension", record("extension"))

    def ledger(_home: object) -> _Ledger:
        return _Ledger(active=state["active"])

    monkeypatch.setattr(authority, "load_ledger", ledger)
    monkeypatch.setattr(authority, "require_ledger", ledger)
    monkeypatch.setattr(authority, "ensure_groups", record("groups"))
    monkeypatch.setattr(authority, "ensure_monitor", record("monitor"))
    monkeypatch.setattr(authority, "retire_legacy_logins", record("retire-legacy"))
    monkeypatch.setattr(authority, "create_ledger", record("ledger"))
    monkeypatch.setattr(authority, "ensure_pooler_admin", record("pooler-admin"))
    monkeypatch.setattr(authority, "mint_generation", record("mint"))
    monkeypatch.setattr(authority, "check_invariant", record("invariant"))
    monkeypatch.setattr(authority, "verify_generation", record("verify"))

    def activate(*_a: object) -> None:
        calls.append("activate")
        state["active"] = True

    monkeypatch.setattr(authority, "activate", activate)
    monkeypatch.setattr(health_preflight, "probe_redis", record("redis-probe"))
    monkeypatch.setattr("cli.start_identity.mark_phase", record("provisioned"))

    def needs_provision(_home: object) -> bool:
        if state["birth"]:
            state["active"] = False
        return state["birth"]

    monkeypatch.setattr("cli.start_identity.needs_provision", needs_provision)
    calls.append("start")
    return recorded


def test_ordinary_start_regrants_and_checks_before_the_pooler(plane: _Plane) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    complete_gateway_data_plane()
    assert plane.calls == [
        "start",
        "extension",
        "memory-vectors",
        "groups",
        "monitor",
        "invariant",
        "pooler",
        "prove-logins",
        "adopt",
        "redis-probe",
        "provisioned",
    ]


def test_birth_mints_generation_zero_and_activates_after_the_pooler_proof(plane: _Plane) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    plane.state["birth"] = True
    complete_gateway_data_plane()
    assert plane.calls == [
        "start",
        "extension",
        "memory-vectors",
        "groups",
        "monitor",
        "retire-legacy",
        "ledger",
        "pooler-admin",
        "mint",
        "pooler",
        "prove-logins",
        "verify",
        "activate",
        "adopt",
        "redis-probe",
        "provisioned",
    ]


def test_release_readiness_performs_no_schema_or_grant_writes(plane: _Plane) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    complete_gateway_data_plane(refresh_schema=False)
    assert plane.calls == [
        "start",
        "invariant",
        "pooler",
        "prove-logins",
        "adopt",
        "redis-probe",
        "provisioned",
    ]


def test_invariant_violation_never_starts_pooler_or_marks_provisioned(
    plane: _Plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.cluster import authority
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    def fail(*_a: object, **_kw: object) -> None:
        raise authority.CatalogRefusedError(("foreign grant",))

    monkeypatch.setattr(authority, "check_invariant", fail)
    with pytest.raises(authority.CatalogRefusedError, match="foreign grant"):
        complete_gateway_data_plane()
    assert plane.calls == ["start", "extension", "memory-vectors", "groups", "monitor"]


class _Authority:
    """Records the owner-authority dial `prepare_memory_vectors` must use."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @contextmanager
    def session(self) -> Generator[str]:
        self._calls.append("owner-session")
        yield "owner-connection"


def test_memory_vectors_prepared_as_owner_only_for_pgvector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pgvector table is start-time DDL through the owner authority at the
    provider's dimension; any other backend dials nothing."""
    from base.db import pg_admin
    from cli.commands.data_plane.bringup import prepare_memory_vectors
    from services.derived.memory_indexer.backends import pgvector
    from services.derived.memory_indexer.embeddings import factory

    calls: list[str] = []
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: _Authority(calls))

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    monkeypatch.setattr(pgvector, "prepare_table", prepare)
    monkeypatch.setattr(factory, "get_provider", lambda: SimpleNamespace(dim=3072))

    monkeypatch.setattr(settings.services, "memory_search_backend", "numpy")
    prepare_memory_vectors()
    assert calls == []

    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    prepare_memory_vectors()
    assert calls == ["owner-session", "prepare:owner-connection:3072"]


def test_remote_plane_prepares_memory_vectors_through_its_provider_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-managed plane has no local owner authority; its provider URL
    carries the table DDL, exactly as it carries the plane's migrations."""
    from base.db import pg_admin
    from cli.commands.data_plane.bringup import prepare_memory_vectors
    from services.derived.memory_indexer.backends import pgvector
    from services.derived.memory_indexer.embeddings import factory

    calls: list[str] = []

    @contextmanager
    def provider(**kwargs: object) -> Generator[str]:
        calls.append(f"provider:{kwargs}")
        yield "provider-connection"

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://owner@db.example/ava")
    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    patch_database(monkeypatch, connect=provider)
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: pytest.fail("no local admin"))
    monkeypatch.setattr(pgvector, "prepare_table", prepare)
    monkeypatch.setattr(factory, "get_provider", lambda: SimpleNamespace(dim=768))

    prepare_memory_vectors()

    assert calls == ["provider:{'direct': True}", "prepare:provider-connection:768"]


@pytest.mark.parametrize(
    ("secret", "missing", "reason"),
    [
        ("bearer-only", "redis_admin_password", "no conversion exists"),
        ("bearer-only", "redis_password", "no conversion exists"),
        ("", "redis_admin_password", "no conversion exists"),
        ("", "redis_password", "no conversion exists"),
    ],
)
def test_storage_refuses_missing_credentials_before_effects(
    monkeypatch: pytest.MonkeyPatch, secret: str, missing: str, reason: str
) -> None:
    """Redis always authenticates, so an empty bearer does not excuse missing
    Redis credentials: a home born without them is refused before any native
    effect. Postgres needs no credential here: its
    administrator is the OS user over the owner-only socket."""
    credentials = {"redis_admin_password": "admin-value", "redis_password": "runtime-value"}
    credentials[missing] = ""
    monkeypatch.setattr(
        _ci,
        "_start_pg",
        lambda *_a: pytest.fail("storage started before validation"),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(ValueError, match=reason):
        _ci.ensure_cluster_storage(
            pg_port=5433,
            redis_port=6380,
            cluster_secret=secret,
            redis_user="ava",
            **credentials,
        )
