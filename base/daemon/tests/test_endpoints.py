"""`ServiceEndpoints`: the table is the settings-and-home facts of `health_port` / `pid_path`,
read once at construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import health_port
from base.host.env.registry import health_port_env_aliases
from base.paths import pid_path


def test_every_health_daemon_has_an_endpoint_matching_the_ambient_helpers() -> None:
    table = ServiceEndpoints.from_settings()
    assert {e.name for e in table} == set(health_port_env_aliases())
    for endpoint in table:
        assert endpoint.health_port == health_port(endpoint.name)
        assert endpoint.pidfile == pid_path(endpoint.name)


def test_a_port_override_reaches_the_row_and_only_that_row(monkeypatch: pytest.MonkeyPatch) -> None:
    before = ServiceEndpoints.from_settings()
    monkeypatch.setattr(settings.services, "labeler_health_port", 18765)
    after = ServiceEndpoints.from_settings()
    assert after.of("labeler").health_port == 18765
    assert after.of("im_bridge") == before.of("im_bridge")


def test_the_table_does_not_follow_later_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    table = ServiceEndpoints.from_settings()
    port = table.of("labeler").health_port
    monkeypatch.setattr(settings.services, "labeler_health_port", port + 1)
    assert table.of("labeler").health_port == port


def test_the_pidfile_lives_under_the_homes_run_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert (
        ServiceEndpoints.from_settings().of("labeler").pidfile == tmp_path / "run" / "labeler.pid"
    )


def test_a_healthz_url_names_the_host_the_caller_reaches_it_from() -> None:
    endpoint = ServiceEndpoint("x", 8123, Path("/run/x.pid"))
    assert endpoint.healthz_url() == "http://127.0.0.1:8123/healthz"
    assert endpoint.healthz_url("localhost") == "http://localhost:8123/healthz"


def test_an_unregistered_daemon_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        ServiceEndpoints.from_settings().of("no_such_daemon")
