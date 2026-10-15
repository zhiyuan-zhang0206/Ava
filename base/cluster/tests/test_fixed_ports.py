from __future__ import annotations

import re
from typing import get_type_hints
from urllib.parse import urlsplit

import pytest

from base import cluster
from base.cluster import ports as cluster_ports
from base.config import FIELD_INFOS, get_field
from base.host.env import registry
from base.host.env.port_table import FIXED_PORTS

_PRIVATE_TEST_PORTS_START = 21000


def test_fixed_ports_carry_the_production_values() -> None:
    assert FIXED_PORTS["gateway"] == 8000
    assert FIXED_PORTS["frontend"] == 3000
    assert FIXED_PORTS["app"] == 3001
    assert FIXED_PORTS["postgres"] == 5433
    assert FIXED_PORTS["redis"] == 6380
    assert FIXED_PORTS["pgbouncer"] == 6433


@pytest.mark.parametrize(
    ("slot", "port"), [("gateway_watchdog", 8119), ("agent_runner_watchdog", 8120)]
)
def test_the_watchdog_slots_no_daemon_binds_are_not_in_the_table(slot: str, port: int) -> None:
    assert slot not in FIXED_PORTS
    assert port not in FIXED_PORTS.values()
    assert slot not in get_type_hints(cluster.ClusterPorts)
    assert slot not in registry.health_port_env_aliases()
    assert f"{slot}_health_port" not in FIELD_INFOS


def test_fixed_ports_are_unique() -> None:
    holders: dict[int, list[str]] = {}
    for svc, port in FIXED_PORTS.items():
        holders.setdefault(port, []).append(svc)
    collisions = {port: svcs for port, svcs in holders.items() if len(svcs) > 1}
    assert not collisions, f"ports shared by more than one service: {collisions}"


def test_fixed_ports_sit_below_every_source_of_test_ports() -> None:
    assert max(FIXED_PORTS.values()) < _PRIVATE_TEST_PORTS_START


def test_the_recorded_ports_type_names_exactly_the_table() -> None:
    assert set(get_type_hints(cluster.ClusterPorts)) == set(FIXED_PORTS)


def test_a_new_home_records_the_table_as_its_own_copy() -> None:
    ports = cluster_ports.new_home_ports()
    assert ports == FIXED_PORTS
    ports["gateway"] = 1
    assert FIXED_PORTS["gateway"] == 8000


def test_settings_defaults_match_the_table() -> None:
    port_fields = {
        "gateway_port": "gateway",
        "browser_cdp_port": "browser",
        "permissions_helper_port": "permissions_helper",
        "memory_search_port": "memory_search",
    }
    for field, slot in port_fields.items():
        assert FIELD_INFOS[field].default == FIXED_PORTS[slot], field
    url_fields = {
        "gateway_health_url": "gateway",
        "frontend_healthcheck_url": "frontend",
        "memory_search_uri": "memory_search",
    }
    for field, slot in url_fields.items():
        default = FIELD_INFOS[field].default
        assert isinstance(default, str)
        assert urlsplit(default).port == FIXED_PORTS[slot], field


def test_the_session_holds_no_table_port_in_any_setting(process_config: object) -> None:
    table = set(FIXED_PORTS.values())
    holders: dict[str, object] = {}
    for name in sorted(FIELD_INFOS):
        value = get_field(name)
        if isinstance(value, int) and not isinstance(value, bool) and value in table:
            holders[name] = value
        elif isinstance(value, str):
            for match in re.finditer(r"://([^/\s:@]*):(\d{2,5})(?=[/\s]|$)", value):
                if int(match.group(2)) in table and not match.group(1).endswith(".invalid"):
                    holders[name] = value
    assert not holders, f"settings of the test session still hold a fixed-table port: {holders}"
