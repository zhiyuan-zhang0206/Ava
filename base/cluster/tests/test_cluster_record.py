import json
from dataclasses import asdict
from pathlib import Path
from typing import cast

from base import cluster


def _record(home: Path) -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"gateway": 18000, "frontend": 18001}),
        gateway_home=str(home),
        created_at="2026-06-01T00:00:00Z",
        data_plane_host="db.internal",
    )


def test_get_record_reads_the_homes_own_start_intent(tmp_path: Path) -> None:
    """A home describes itself: its record is the `record` of its start intent,
    and nothing outside the home is read."""
    home = tmp_path / ".ava-t1"
    home.mkdir()
    rec = _record(home)
    (home / cluster.INTENT_NAME).write_text(json.dumps({"record": asdict(rec), "phase": "ready"}))

    assert cluster.get_record(home) == rec


def test_get_record_is_none_for_a_home_without_a_start_intent(tmp_path: Path) -> None:
    assert cluster.get_record(tmp_path / "never-born") is None


def test_get_record_is_none_for_a_unit_without_the_gateway(tmp_path: Path) -> None:
    """A remote agent-runner owns no data plane, so its intent carries no record."""
    home = tmp_path / ".ava-runner"
    home.mkdir()
    (home / cluster.INTENT_NAME).write_text(json.dumps({"record": None, "phase": "ready"}))

    assert cluster.get_record(home) is None
