"""The fixed port table: its shape, what a new home records, and the test session's
isolation from it.

A host runs one cluster, so every new home records the same table
(`base.host.env.port_table`) and a unit whose `.env` names no port binds it too.
Those are the numbers the operator's cluster on a development box is already bound
to, so nothing a test binds or dials may be one of them: the tests below keep the
table apart from every source of test ports, and fail when a setting of the test
session still holds a table port.
"""

from __future__ import annotations

import re
from typing import get_type_hints
from urllib.parse import urlsplit

from base import cluster
from base.cluster import ports as cluster_ports
from base.config import FIELD_INFOS, get_field
from base.host.env.port_table import FIXED_PORTS

# The private range the suite draws its own listening ports from when the kernel's
# ephemeral range is not wanted (cli/commands/data_plane/tests/test_pooler_stop.py).
_PRIVATE_TEST_PORTS_START = 21000


def test_fixed_ports_carry_the_production_values() -> None:
    assert FIXED_PORTS["gateway"] == 8000
    assert FIXED_PORTS["frontend"] == 3000
    assert FIXED_PORTS["app"] == 3001
    assert FIXED_PORTS["milvus"] == 19530
    assert FIXED_PORTS["postgres"] == 5433
    assert FIXED_PORTS["redis"] == 6380
    assert FIXED_PORTS["pgbouncer"] == 6433


def test_fixed_ports_are_unique() -> None:
    """No two services may share a port.

    These are the ports a unit whose `.env` predates a key ACTUALLY BINDS
    (`daemon.health.DEFAULT_PORTS` is derived from this table), so a duplicate
    is not a cosmetic clash — the two daemons fight over one socket on every
    existing unit. The table is deliberately not in any order (`ops` moved
    off 8106 to dodge the Windows iphlpsvc grab), so "next number after the last
    line" is not a safe way to add one: agent_host was written as 8113, which
    `ops` already held, and nothing caught it until a boot log printed the same
    port twice.
    """
    holders: dict[int, list[str]] = {}
    for svc, port in FIXED_PORTS.items():
        holders.setdefault(port, []).append(svc)
    collisions = {port: svcs for port, svcs in holders.items() if len(svcs) > 1}
    assert not collisions, f"ports shared by more than one service: {collisions}"


def test_fixed_ports_sit_below_every_source_of_test_ports() -> None:
    """The kernel's ephemeral range starts at 32768 (Linux) or 49152 (macOS) and the
    suite's private range at 21000, so a table that stays below 21000 can never be
    handed to a test by either: no test port can equal a port a live cluster binds."""
    assert max(FIXED_PORTS.values()) < _PRIVATE_TEST_PORTS_START


def test_the_recorded_ports_type_names_exactly_the_table() -> None:
    assert set(get_type_hints(cluster.ClusterPorts)) == set(FIXED_PORTS)


def test_a_new_home_records_the_table_as_its_own_copy() -> None:
    ports = cluster_ports.new_home_ports()
    assert ports == FIXED_PORTS
    ports["gateway"] = 1
    assert FIXED_PORTS["gateway"] == 8000


def test_settings_defaults_match_the_table() -> None:
    """A unit whose `.env` names no port reads the `Settings` default, and a new
    home's `.env` is written from the table: the two copies of each number agree."""
    port_fields = {
        "gateway_port": "gateway",
        "milvus_port": "milvus",
        "browser_cdp_port": "browser",
        "permissions_helper_port": "permissions_helper",
        "memory_search_port": "memory_search",
    }
    for field, slot in port_fields.items():
        assert FIELD_INFOS[field].default == FIXED_PORTS[slot], field
    url_fields = {
        "gateway_health_url": "gateway",
        "frontend_healthcheck_url": "frontend",
        "milvus_uri": "milvus",
        "memory_search_uri": "memory_search",
    }
    for field, slot in url_fields.items():
        default = FIELD_INFOS[field].default
        assert isinstance(default, str)
        assert urlsplit(default).port == FIXED_PORTS[slot], field


def test_the_session_holds_no_table_port_in_any_setting() -> None:
    """The env block of `tests/fixtures/env_bootstrap.py` pins every setting that
    defaults to a table port to a kernel-assigned one. A setting added later with a
    table default, and not pinned there, fails here on any machine — with no
    cluster running on it — instead of dialing the operator's cluster on the
    machines that have one."""
    table = set(FIXED_PORTS.values())
    holders: dict[str, object] = {}
    for name in sorted(FIELD_INFOS):
        value = get_field(name)
        if isinstance(value, int) and not isinstance(value, bool) and value in table:
            holders[name] = value
        elif isinstance(value, str):
            for match in re.finditer(r"://([^/\s:@]*):(\d{2,5})(?=[/\s]|$)", value):
                # `.invalid` never resolves (RFC 2606): it cannot reach a cluster.
                if int(match.group(2)) in table and not match.group(1).endswith(".invalid"):
                    holders[name] = value
    assert not holders, f"settings of the test session still hold a fixed-table port: {holders}"
