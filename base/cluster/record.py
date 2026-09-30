"""A cluster home's own record: the ports and data-plane host it was born with.

The record lives in the home it describes, as the `record` of the home's start
intent (`$AVA_HOME/start-intent.json`, written by `cli.start_identity` before
any effect). A home describes only itself: nothing here lists, looks up or
reserves against another cluster. A unit without the gateway capability owns no
data plane, so its intent carries no record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from base import cluster

INTENT_NAME = "start-intent.json"


@dataclass(frozen=True)
class ClusterRecord:
    ports: cluster.ClusterPorts
    gateway_home: str
    created_at: str
    # The host this cluster's data-plane URLs are DERIVED at (birth); empty =
    # loopback (127.0.0.1), the single-box posture. Stored on the record because
    # derivation happens at birth, before the home's `.env` exists; the birth path
    # snapshots it from `AVA_DATA_PLANE_HOST` — see `cli.start_identity` and
    # `base.cluster.derive.per_cluster_base_urls`. External data plane: Task #1752.
    data_plane_host: str = ""


def get_record(home: Path) -> ClusterRecord | None:
    """This home's record, read from its own start intent.

    None when the home has no start intent (not born) or its intent carries no
    record (a unit without the gateway capability). Nothing outside the home is
    consulted.
    """
    path = Path(home).expanduser() / INTENT_NAME
    if not path.exists():
        return None
    record = json.loads(path.read_text())["record"]
    return None if record is None else ClusterRecord(**record)
