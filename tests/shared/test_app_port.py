"""`record_app_port` — derive the Next.js app port for records born before the
`app` slot existed (same pattern as `record_pgbouncer_port`)."""

from __future__ import annotations

from typing import cast

from shared import cluster
from shared.cluster import ClusterPorts, ClusterRecord, record_app_port


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


def test_app_port_present_is_returned_verbatim() -> None:
    rec = _rec("/x/.ava-dev", {"gateway": 18000, "frontend": 18001, "app": 18099})
    assert record_app_port(rec) == 18099


def test_app_port_derived_for_default_home() -> None:
    """The prod default home's saved record predates the slot -> the fixed legacy 3001."""
    rec = _rec(str(cluster.default_home()), {"gateway": 8000, "frontend": 3000})
    assert record_app_port(rec) == cluster.LEGACY_AVA_PORTS["app"] == 3001


def test_app_port_derived_for_block_home() -> None:
    """An allocated cluster without the slot -> block base + app offset, always
    inside the cluster's own reserved block (never a collision with a
    neighbouring cluster)."""
    rec = _rec("/x/.ava-dev", {"gateway": 18032, "frontend": 18033})
    assert record_app_port(rec) == 18032 + cluster.PORT_OFFSETS["app"]
