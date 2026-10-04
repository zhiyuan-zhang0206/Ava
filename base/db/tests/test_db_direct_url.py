"""The one-DB-URL design: AVA_DB_URL's port is chosen at generation by the
pgbouncer toggle, the admin plane derives the direct URL from this home's
cluster record, and the pooler port is a record fact only (no AVA_PGBOUNCER_PORT
env key). Tests `base.db.direct_db_url`.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from base import cluster, config
from base import db as db_module
from base.cluster import ClusterPorts, ClusterRecord
from base.config.data_plane import DataPlaneSettings
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

_POOLED = "postgresql://ava_main:sek@127.0.0.1:6433/ava_main"
_DIRECT = "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"


def test_pgbouncer_enabled_defaults_on() -> None:
    """The product default is ON (own-instance clusters pool by default; a fleet of a few
    hundred agents at 2 conns each would blow a 500 max_connections direct). The test suite
    pins it off in conftest for determinism, so assert the field default directly rather
    than the loaded value."""
    assert DataPlaneSettings.model_fields["pgbouncer_enabled"].default is True


def test_no_pgbouncer_port_field_on_the_settings_surface() -> None:
    """F8b: the pooler port is a record fact, not a Settings field — a normal
    process sees only AVA_DB_URL (whose port the toggle chose at generation)."""
    assert "pgbouncer_port" not in DataPlaneSettings.model_fields


# ── direct_db_url: the admin plane's never-pooled dial ──


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


def _set(monkeypatch: pytest.MonkeyPatch, *, db_url: str, rec: ClusterRecord | None) -> None:

    from base import paths

    monkeypatch.setattr(config.settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(cluster, "get_record", lambda home: rec if str(home) == _HOME else None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths, "ava_home", lambda: Path(_HOME))


_HOME = "/x/.ava-t"
# A record carrying the fixed table's data-plane ports.
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


def test_direct_db_url_never_rewrites_placeholder_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentinel must stay byte-identical for the connect guard."""
    _set(monkeypatch, db_url=PLACEHOLDER_DB_URL, rec=_PG_REC)
    assert db_module.direct_db_url() == PLACEHOLDER_DB_URL


def test_direct_db_url_keeps_the_url_without_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No record (a home without the gateway) -> AVA_DB_URL as-is rather than guessing."""
    _set(monkeypatch, db_url=_POOLED, rec=None)
    assert db_module.direct_db_url() == _POOLED


def test_direct_db_url_leaves_operator_standin_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL naming neither this cluster's pg nor its pooler port (a dev-only
    stand-in) is not rewritten — converge normalizes only the two cluster ports."""
    _set(monkeypatch, db_url="postgresql://ava:dev@localhost:5432/ava", rec=_PG_REC)
    assert db_module.direct_db_url() == "postgresql://ava:dev@localhost:5432/ava"


def test_direct_db_url_uses_the_ports_the_record_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The swap follows whatever ports the record carries, not the table's."""
    rec = _rec(
        "/x/.ava-dev",
        {"gateway": 18000, "postgres": 18011, "redis": 18012, "pgbouncer": 18013},
    )
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
    other = _rec(
        "/x/.ava-other",
        {"gateway": 19000, "postgres": 19011, "redis": 19012, "pgbouncer": 19013},
    )
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


def test_direct_db_url_remote_host_ignores_the_home_record(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """A URL naming a REMOTE host (a split runner dialing the gateway, or a
    remote/SaaS plane — Task #1752) must not be resolved against this home's
    record even when its port happens to collide — the swap would
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


def test_direct_db_url_split_runner_falls_back_with_an_info_line(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """The split-runner case: this home's record does not explain the URL's port
    (the gateway's pooler port is a fact of the GATEWAY home's record). The URL is
    returned as-is — runner boot must not break — and the expected degraded dial
    is logged at INFO, never silent and never a warning (every start logs one;
    2026-10-03 triage). The runner's URL names its own gateway
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
    lines = [r for r in loguru_records if "this home's record" in r["message"]]
    assert [r["level"].name for r in lines] == ["INFO"]


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
