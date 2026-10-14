"""Direct database resolution consumes the root's lazy gateway and host readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from base.cluster.dataplane import direct_url
from base.cluster.dataplane.direct_url import ClusterDirectUrl, direct_db_url
from base.cluster.ports import ClusterPorts
from base.cluster.record import ClusterRecord
from base.config import DataPlaneSettings
from base.db import Database, DbConfig
from base.db.code_version_gate import ProcessDbGate
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

_POOLED = "postgresql://ava_main:sek@127.0.0.1:6433/ava_main"
_DIRECT = "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"
_HOME = "/x/.ava-t"


def test_pgbouncer_enabled_defaults_on() -> None:
    assert DataPlaneSettings.model_fields["pgbouncer_enabled"].default is True


def test_no_pgbouncer_port_field_on_the_settings_surface() -> None:
    assert "pgbouncer_port" not in DataPlaneSettings.model_fields


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


_PG_REC = _rec(_HOME, {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433})


def _set_record(monkeypatch: pytest.MonkeyPatch, rec: ClusterRecord | None) -> None:
    from base import paths

    monkeypatch.setattr(direct_url, "get_record", lambda home: rec if str(home) == _HOME else None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths, "ava_home", lambda: Path(_HOME))


def _config(url: str, *, pooling: bool = True) -> DbConfig:
    return DbConfig(
        db_url=url,
        db_sslmode="",
        db_pool_min_size=1,
        db_pool_max_size=2,
        pgbouncer_enabled=pooling,
    )


def _unused_reader() -> str:
    raise AssertionError("this resolution must not read the dependency")


def test_direct_handle_uses_owned_lazy_readers_and_preserves_short_circuits(
    database_gate: ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_record(monkeypatch, _PG_REC)
    reads: list[str] = []

    def local_host() -> str:
        reads.append("host")
        return "OWNED-HOST"

    def handle(url: str) -> Database:
        return Database(
            _config(url),
            direct_url=ClusterDirectUrl(local_host, gateway_url_reader=_unused_reader),
            gate=database_gate,
        )

    assert handle(PLACEHOLDER_DB_URL).direct_url() == PLACEHOLDER_DB_URL
    assert handle("not-a-port").direct_url() == "not-a-port"
    assert handle(_POOLED).direct_url() == _DIRECT
    assert reads == []
    assert handle(_POOLED.replace("127.0.0.1", "owned-host")).direct_url() == _DIRECT.replace(
        "127.0.0.1", "owned-host"
    )
    assert reads == ["host"]


@pytest.mark.parametrize(
    "url",
    [
        PLACEHOLDER_DB_URL,
        "postgresql://host:bad/ava",
        "postgresql://host/ava",
        "postgresql://[/ava",
    ],
)
def test_direct_url_short_circuits_read_neither_dependency(url: str) -> None:
    assert (
        direct_db_url(_config(url), gateway_url_reader=_unused_reader, local_host=_unused_reader)
        == url
    )


def test_direct_host_comparison_requires_an_explicit_reader() -> None:
    with pytest.raises(RuntimeError, match="explicit local_host reader"):
        direct_db_url(
            _config(_POOLED.replace("127.0.0.1", "owned-host")), gateway_url_reader=_unused_reader
        )


@pytest.mark.parametrize("url, expected", [(_POOLED, _DIRECT), (_DIRECT, _DIRECT)])
def test_local_record_resolves_without_reading_gateway(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: str
) -> None:
    _set_record(monkeypatch, _PG_REC)
    assert direct_db_url(_config(url), gateway_url_reader=_unused_reader) == expected


@pytest.mark.parametrize("rec", [None, _PG_REC])
def test_local_unknown_port_preserves_url_without_reading_gateway(
    monkeypatch: pytest.MonkeyPatch, rec: ClusterRecord | None, loguru_records: list[dict[str, Any]]
) -> None:
    _set_record(monkeypatch, rec)
    url = "postgresql://ava:dev@localhost:5432/ava"
    assert direct_db_url(_config(url), gateway_url_reader=_unused_reader) == url
    assert [record["level"].name for record in loguru_records] == ["INFO"]


def test_pooler_url_without_a_record_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_record(monkeypatch, None)
    assert direct_db_url(_config(_POOLED), gateway_url_reader=_unused_reader) == _POOLED


@pytest.mark.parametrize("pg_port, pooler_port", [(18011, 18013), (19011, 19013)])
def test_direct_db_url_uses_the_ports_the_record_names(
    monkeypatch: pytest.MonkeyPatch, pg_port: int, pooler_port: int
) -> None:
    rec = _rec(
        "/x/.ava-other",
        {"gateway": 18000, "postgres": pg_port, "redis": 18012, "pgbouncer": pooler_port},
    )
    _set_record(monkeypatch, rec)
    url = f"postgresql://ava:sek@127.0.0.1:{pooler_port}/ava?sslmode=prefer"
    assert direct_db_url(_config(url), gateway_url_reader=_unused_reader) == (
        f"postgresql://ava:sek@127.0.0.1:{pg_port}/ava?sslmode=prefer"
    )


@pytest.mark.parametrize(
    "host, gateway_url, logs",
    [
        ("10.0.0.9", "http://127.0.0.1:18000", False),
        ("10.0.0.9", "http://10.0.0.9:18000", True),
        ("DB.EXAMPLE", "https://db.example:18000", True),
        ("[2001:db8::9]", "http://[2001:DB8::9]:18000", True),
        ("db.example", "", False),
    ],
)
def test_remote_fallback_logs_only_for_its_gateway(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    host: str,
    gateway_url: str,
    logs: bool,
) -> None:
    _set_record(monkeypatch, _PG_REC)
    reads: list[str] = []

    def local_host() -> str:
        reads.append("host")
        return "owned-host"

    def gateway_reader() -> str:
        reads.append("gateway")
        return gateway_url

    url = f"postgresql://ava_main:sek@{host}:6433/ava_main"
    resolver = ClusterDirectUrl(local_host, gateway_url_reader=gateway_reader)
    assert reads == []
    assert resolver(_config(url)) == url
    assert reads == ["host", "gateway"]
    lines = [record for record in loguru_records if "this home's record" in record["message"]]
    assert [record["level"].name for record in lines] == (["INFO"] if logs else [])


@pytest.mark.parametrize("host", ["localhost", "db.example"])
def test_pooling_off_does_not_read_gateway_or_log(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]], host: str
) -> None:
    _set_record(monkeypatch, None)
    url = f"postgresql://ava:dev@{host}:5432/ava"
    assert (
        direct_db_url(
            _config(url, pooling=False),
            gateway_url_reader=_unused_reader,
            local_host=lambda: "owned-host",
        )
        == url
    )
    assert loguru_records == []


def test_resolvers_read_their_own_live_gateway_values(loguru_records: list[dict[str, Any]]) -> None:
    gateways = {"first": "http://db.example:8000", "second": "http://elsewhere:8000"}
    first = ClusterDirectUrl(lambda: "owned-host", gateway_url_reader=lambda: gateways["first"])
    second = ClusterDirectUrl(lambda: "owned-host", gateway_url_reader=lambda: gateways["second"])
    config = _config("postgresql://ava:dev@db.example:6433/ava")
    assert first(config) == config.db_url
    assert len(loguru_records) == 1
    assert second(config) == config.db_url
    assert len(loguru_records) == 1
    gateways["first"] = "http://elsewhere:8000"
    gateways["second"] = "http://db.example:8000"
    assert first(config) == config.db_url
    assert len(loguru_records) == 1
    assert second(config) == config.db_url
    assert len(loguru_records) == 2


def test_gateway_reader_failure_propagates() -> None:
    failure = AttributeError("gateway domain missing")

    def unavailable() -> str:
        raise failure

    with pytest.raises(AttributeError) as caught:
        direct_db_url(
            _config("postgresql://ava:dev@db.example:6433/ava"),
            gateway_url_reader=unavailable,
            local_host=lambda: "owned-host",
        )
    assert caught.value is failure
