"""Two-cluster isolation: ports (including each cluster's own pg/redis instance).

Simulates two sequential install-time cluster births on a single host (via the
real `_ensure_record`) and asserts the core isolation guarantee: the two
home-keyed records are completely disjoint — distinct port blocks, so distinct
pg/redis instances. (There are no per-cluster db names to compare: every
cluster's own single-tenant instance uses the fixed `ava` identifier, carried by
its `.env` URLs as data.)

The full end-to-end verification (real docker + enroll + agent spawn
through the ops server) is a manual step documented in the runbook; it requires
docker and live host ports and is therefore not run in CI.
"""

from pathlib import Path
from typing import cast

import pytest

from cli.commands.cluster_lifecycle import _ensure_record
from shared import cluster
from shared.port_block import BLOCK_SIZE


def test_two_clusters_disjoint_ports_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two non-default clusters allocated sequentially get disjoint resources."""
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType]
        cluster, "registry_path", lambda: tmp_path / "clusters.json"
    )
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType]
        cluster,
        "_port_free",
        lambda _p: True,  # pyright: ignore[reportUnknownArgumentType]
    )

    h1, h2 = tmp_path / ".ava-t1", tmp_path / ".ava-t2"
    r1, created1 = _ensure_record(h1)
    r2, created2 = _ensure_record(h2)
    assert created1 and created2
    p1 = cast("dict[str, int]", r1.ports)
    p2 = cast("dict[str, int]", r2.ports)

    # Ports: the two service maps must share no port numbers.
    assert set(p1.values()).isdisjoint(set(p2.values())), (
        f"port overlap: {set(p1.values()) & set(p2.values())}"
    )

    # Each cluster's own pg/redis ports are distinct — separate data-plane instances.
    assert p1["postgres"] != p2["postgres"]
    assert p1["redis"] != p2["redis"]

    # Port blocks differ by exactly BLOCK_SIZE (19): t1 gets base 18000, t2 gets 18019.
    base1 = min(p1.values())
    base2 = min(p2.values())
    assert base2 - base1 == BLOCK_SIZE, f"expected block gap {BLOCK_SIZE}, got {base2 - base1}"

    # Both records survive round-trip through the home-keyed registry.
    final = cluster.load_registry()
    assert final.keys() == {str(h1), str(h2)}
    assert final[str(h1)] == r1
    assert final[str(h2)] == r2
