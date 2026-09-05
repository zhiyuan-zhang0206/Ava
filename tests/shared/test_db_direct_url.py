"""The one-DB-URL design: AVA_DB_URL's port is chosen at generation by the
pgbouncer toggle, the admin plane derives the direct URL from the registry
record, and the pooler port is a registry fact only (no AVA_PGBOUNCER_PORT env
key). Tests `shared.db.direct_db_url` + the record port derivations.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from shared import cluster, config
from shared import db as db_module
from shared.cluster import (
    ClusterPorts,
    ClusterRecord,
    record_pgbouncer_port,
    record_postgres_port,
    record_redis_port,
)
from shared.config.data_plane import DataPlaneSettings
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.port_block import PORT_OFFSETS

_POOLED = "postgresql://ava_main:sek@127.0.0.1:6433/ava_main"
_DIRECT = "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"


def test_pgbouncer_enabled_defaults_on() -> None:
    """The product default is ON (own-instance clusters pool by default; a fleet of a few
    hundred agents at 2 conns each would blow a 500 max_connections direct). The test suite
    pins it off in conftest for determinism, so assert the field default directly rather
    than the loaded value."""
    assert DataPlaneSettings.model_fields["pgbouncer_enabled"].default is True


def test_no_pgbouncer_port_field_on_the_settings_surface() -> None:
    """F8b: the pooler port is a registry fact, not a Settings field — a normal
    process sees only AVA_DB_URL (whose port the toggle chose at generation)."""
    assert "pgbouncer_port" not in DataPlaneSettings.model_fields


# ── direct_db_url: the admin plane's never-pooled dial ──


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


def _set(monkeypatch: pytest.MonkeyPatch, *, db_url: str, rec: ClusterRecord | None) -> None:

    from shared import paths

    monkeypatch.setattr(config.settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(cluster, "load_registry", lambda: {_HOME: rec} if rec is not None else {})
    monkeypatch.setattr(paths, "ava_home", lambda: Path(_HOME))


_HOME = "/x/.ava-t"
# A legacy-shaped record (the prod default-home block): explicit pgbouncer key.
_PG_REC = _rec(_HOME, {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433})


def test_direct_db_url_swaps_pooler_port_to_direct_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_DB_URL carries the pooler port (pooling on) -> the admin plane dials
    the same URL with the port swapped to the record's direct Postgres port."""
    _set(monkeypatch, db_url=_POOLED, rec=_PG_REC)
    assert db_module.direct_db_url() == _DIRECT


def test_direct_db_url_passes_through_when_already_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pooling off: AVA_DB_URL already names Postgres -> returned verbatim."""
    _set(monkeypatch, db_url=_DIRECT, rec=_PG_REC)
    assert db_module.direct_db_url() == _DIRECT


def test_direct_db_url_never_rewrites_unanchored_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentinel must stay byte-identical for the connect guard."""
    _set(monkeypatch, db_url=UNANCHORED_DB_SENTINEL, rec=_PG_REC)
    assert db_module.direct_db_url() == UNANCHORED_DB_SENTINEL


def test_direct_db_url_falls_back_without_registry_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No record (an unusual host) -> AVA_DB_URL as-is rather than guessing."""
    _set(monkeypatch, db_url=_POOLED, rec=None)
    assert db_module.direct_db_url() == _POOLED


def test_direct_db_url_leaves_operator_standin_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL naming neither this cluster's pg nor its pooler port (a dev-only
    stand-in) is not rewritten — converge normalizes only the two cluster ports."""
    _set(monkeypatch, db_url="postgresql://ava:dev@localhost:5432/ava", rec=_PG_REC)
    assert db_module.direct_db_url() == "postgresql://ava:dev@localhost:5432/ava"


def test_direct_db_url_allocated_cluster_uses_block_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allocated cluster: pooler = base+13, pg = base+11."""
    rec = _rec("/x/.ava-dev", {"gateway": 18000, "postgres": 18011, "redis": 18012})
    _set(
        monkeypatch,
        db_url="postgresql://ava:sek@127.0.0.1:18013/ava",
        rec=rec,
    )
    assert db_module.direct_db_url() == "postgresql://ava:sek@127.0.0.1:18011/ava"


# ── F-S5-9: the host-record assumption ──


def test_direct_db_url_swaps_any_local_record_pooler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL names ANOTHER local cluster's pooler (multi-cluster box, or a
    worktree pointing at a sibling cluster) — the admin plane must dial THAT
    cluster's real Postgres, so the swap uses the record that owns the port,
    not this home's record."""
    other = _rec("/x/.ava-other", {"gateway": 19000, "postgres": 19011, "redis": 19012})
    _set(
        monkeypatch,
        db_url="postgresql://ava:sek@127.0.0.1:19013/ava",
        rec=other,
    )
    assert db_module.direct_db_url() == "postgresql://ava:sek@127.0.0.1:19011/ava"


def test_direct_db_url_already_direct_names_a_local_pg_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pooling off: AVA_DB_URL carries a local record's direct pg port (not the
    pooler's) — returned verbatim, no swap, no warning."""
    _set(monkeypatch, db_url=_DIRECT, rec=_PG_REC)
    assert db_module.direct_db_url() == _DIRECT


def test_direct_db_url_remote_host_ignores_local_registry(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """A URL naming a REMOTE host (a split runner dialing the gateway, or a
    remote/SaaS plane — Task #1752) must not be resolved against local records
    even when a local record's port happens to collide — the swap would
    mis-route to this box's own Postgres. The URL passes through SILENTLY: a
    foreign host has no local pooler, so the "routes through PgBouncer"
    warning would be factually wrong noise on every admin-plane dial."""
    monkeypatch.setattr(config.settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://127.0.0.1:18000")
    _set(
        monkeypatch,
        db_url="postgresql://ava_main:sek@10.0.0.9:6433/ava_main",
        rec=_PG_REC,
    )
    got = db_module.direct_db_url()
    assert got == "postgresql://ava_main:sek@10.0.0.9:6433/ava_main"
    assert not any("direct_db_url" in r["message"] for r in loguru_records)


def test_direct_db_url_split_runner_falls_back_loudly(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """The split-runner case: no local record explains the URL's port (the
    gateway's pooler port is a fact of the GATEWAY box's registry). The URL is
    returned as-is — runner boot must not break — but the degraded dial is
    logged, never silent. The runner's URL names its own gateway
    (AVA_GATEWAY_URL), which keeps this distinct from a remote/SaaS plane —
    foreign but pooler-less, dialed silently (Task #1752)."""
    monkeypatch.setattr(config.settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "http://10.0.0.9:18000")
    _set(
        monkeypatch,
        db_url="postgresql://ava_main:sek@10.0.0.9:6433/ava_main",
        rec=None,
    )
    got = db_module.direct_db_url()
    assert got == "postgresql://ava_main:sek@10.0.0.9:6433/ava_main"
    assert any("no local registry" in r["message"] for r in loguru_records)


def test_direct_db_url_unknown_port_stays_silent_when_pooling_off(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """Pooling off: the one URL IS the direct Postgres URL by construction; an
    unknown port (an operator stand-in) is genuinely direct — no warning."""
    assert config.settings.data_plane.pgbouncer_enabled is False
    _set(monkeypatch, db_url="postgresql://ava:dev@localhost:5432/ava", rec=None)
    got = db_module.direct_db_url()
    assert got == "postgresql://ava:dev@localhost:5432/ava"
    assert loguru_records == []


# ── data-plane record ports: derive for records saved before a slot ───────────


def test_record_pgbouncer_port_present_is_returned_verbatim() -> None:
    rec = _rec(
        "/x/.ava-dev", {"gateway": 18000, "postgres": 18011, "redis": 18012, "pgbouncer": 18099}
    )
    assert record_pgbouncer_port(rec) == 18099


def test_record_pgbouncer_port_derived_for_default_home() -> None:
    # The prod default home's saved record predates the slot -> the fixed legacy 6433.
    rec = _rec(str(cluster.default_home()), {"gateway": 8000, "postgres": 5433, "redis": 6380})
    assert record_pgbouncer_port(rec) == cluster.LEGACY_AVA_PORTS["pgbouncer"] == 6433


def test_record_pgbouncer_port_derived_for_allocated_cluster() -> None:
    # An allocated cluster derives base(gateway) + offset, inside its own 16-block.
    rec = _rec("/x/.ava-dev", {"gateway": 18000, "postgres": 18011, "redis": 18012})
    assert record_pgbouncer_port(rec) == 18000 + PORT_OFFSETS["pgbouncer"] == 18013


def test_record_postgres_port_derived_for_default_home() -> None:
    """Defensive derive (every real record carries postgres): the default home's
    fixed legacy 5433."""
    rec = _rec(str(cluster.default_home()), {"gateway": 8000})
    assert record_postgres_port(rec) == 5433


def test_record_postgres_port_derived_for_allocated_cluster() -> None:
    rec = _rec("/x/.ava-dev", {"gateway": 18000})
    assert record_postgres_port(rec) == 18000 + PORT_OFFSETS["postgres"] == 18011


def test_record_redis_port_is_a_registry_fact() -> None:
    """Redis's port is carried by the record, not inferred from a URL that may
    use this host's reachable address while Redis remains loopback-only."""
    rec = _rec("/x/.ava-dev", {"gateway": 18000, "redis": 18042})
    assert record_redis_port(rec) == 18042
