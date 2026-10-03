"""Golden: `ServiceEndpoints.from_settings()` reproduces, row by row, the port and pidfile the
ambient `health_port(name)` / `pid_path(name)` lookups returned (the functions it replaced).

The reference below is those two lookups as they were: the override field first, else the fixed
port table; the pidfile as `$AVA_HOME/run/<name>.pid`. A daemon whose row drifted would bind
another port than its roster probe dials, or write a pidfile its identity probe never reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from base.config import get_field, settings
from base.daemon.endpoints import ServiceEndpoints
from base.host.env.registry import health_port_env_aliases
from base.paths import run_dir

# The fixed port table as of the cut-over, spelled out: renumbering a daemon is a decision, so
# it fails here before it reaches a unit whose roster and `.env` still say the old port.
_FIXED_PORTS: Mapping[str, int] = {
    "labeler": 8103,
    "memory_indexer": 8105,
    "heartbeat": 8107,
    "task_maintenance": 8108,
    "events_maintenance": 8109,
    "delivery_watchdog": 8110,
    "im_bridge": 8111,
    "page_server": 8112,
    "ops": 8113,
    "agent_host": 8114,
    "pg_backup": 8116,
    "ttl_reaper": 8121,
    "schedule_manager": 8122,
}


def _old_health_port(name: str) -> int:
    override = get_field(f"{name}_health_port")
    return _FIXED_PORTS[name] if override is None else int(override)


def _old_pid_path(name: str) -> Path:
    return run_dir() / f"{name}.pid"


def _assert_rows_match_the_old_lookups() -> None:
    table = ServiceEndpoints.from_settings()
    assert {endpoint.name for endpoint in table} == set(_FIXED_PORTS)
    for endpoint in table:
        assert endpoint.health_port == _old_health_port(endpoint.name), endpoint.name
        assert endpoint.pidfile == _old_pid_path(endpoint.name), endpoint.name


def test_the_table_names_exactly_the_daemons_with_a_registered_port() -> None:
    assert set(health_port_env_aliases()) == set(_FIXED_PORTS)


def test_default_settings_give_the_fixed_ports_and_run_dir_pidfiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _FIXED_PORTS:
        monkeypatch.setattr(settings.services, f"{name}_health_port", None)
    table = ServiceEndpoints.from_settings()
    assert {e.name: e.health_port for e in table} == _FIXED_PORTS
    _assert_rows_match_the_old_lookups()


def test_overridden_settings_give_the_override_in_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides = {name: 19000 + index for index, name in enumerate(sorted(_FIXED_PORTS))}
    for name, port in overrides.items():
        monkeypatch.setattr(settings.services, f"{name}_health_port", port)
    table = ServiceEndpoints.from_settings()
    assert {e.name: e.health_port for e in table} == overrides
    _assert_rows_match_the_old_lookups()


def test_a_partial_override_moves_only_the_overridden_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _FIXED_PORTS:
        monkeypatch.setattr(settings.services, f"{name}_health_port", None)
    monkeypatch.setattr(settings.services, "ops_health_port", 18113)
    monkeypatch.setattr(settings.services, "agent_host_health_port", 18114)
    table = ServiceEndpoints.from_settings()
    assert {e.name: e.health_port for e in table} == {
        **_FIXED_PORTS,
        "ops": 18113,
        "agent_host": 18114,
    }
    _assert_rows_match_the_old_lookups()


def test_pidfiles_follow_the_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    table = ServiceEndpoints.from_settings()
    assert {e.name: e.pidfile for e in table} == {
        name: tmp_path / "run" / f"{name}.pid" for name in _FIXED_PORTS
    }
