"""`record_app_port` — read the Next.js app port off a registry record (the
default home keeps its fixed legacy value)."""

from __future__ import annotations

from typing import cast

from shared import cluster
from shared.cluster import ClusterPorts, ClusterRecord, record_app_port


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


def test_app_port_present_is_returned_verbatim() -> None:
    rec = _rec("/x/.ava-dev", {"gateway": 18000, "frontend": 18001, "app": 18099})
    assert record_app_port(rec) == 18099


def test_app_port_falls_back_for_default_home() -> None:
    """The prod default home keeps its fixed legacy 3001 even without the key."""
    rec = _rec(str(cluster.default_home()), {"gateway": 8000, "frontend": 3000})
    assert record_app_port(rec) == cluster.LEGACY_AVA_PORTS["app"] == 3001


def test_app_port_missing_on_allocated_record_raises() -> None:
    """An allocated record without the slot is corrupt — the read fails loudly
    rather than guessing a neighbour's port (records are born with the full
    block; only the default home may fall back)."""
    import pytest

    rec = _rec("/x/.ava-dev", {"gateway": 18032, "frontend": 18033})
    with pytest.raises(KeyError):
        record_app_port(rec)
