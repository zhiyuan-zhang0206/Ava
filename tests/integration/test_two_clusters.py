"""Two-cluster isolation: ports (including each cluster's own pg/redis instance).

Simulates two sequential first-start cluster births on a single host (via the
real `prepare_identity`). A home knows no other cluster, so allocation only
probes which ports are bound right now: a second birth beside a running cluster
gets a disjoint block, so a distinct pg/redis instance. (There are no
per-cluster db names to compare: every cluster's own single-tenant instance
uses the fixed `ava` identifier, carried by its `.env` URLs as data.)

The full end-to-end verification (real data planes + a runner's first start +
agent spawn through the ops server) is a manual step documented in the runbook;
it requires live host processes and ports and is therefore not run in CI.
"""

from pathlib import Path
from typing import cast

import pytest

from base import cluster
from base.host.env.port_block import BLOCK_SIZE
from cli.start_identity import IdentityInput, prepare_identity


def _birth(home: Path, checkout: Path) -> dict[str, int]:
    prepare_identity(
        IdentityInput(
            home,
            checkout,
            False,
            frozenset({"gateway", "agent-runner"}),
            {"AVA_MACHINE_NAME": home.name},
        )
    )
    rec = cluster.get_record(home)
    assert rec is not None
    return cast("dict[str, int]", rec.ports)


def test_birth_beside_a_running_cluster_gets_a_disjoint_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first cluster's bound ports push the second birth to the next block."""
    bound: set[int] = set()
    monkeypatch.setattr(cluster, "port_free", lambda port: port not in bound)  # pyright: ignore[reportUnknownArgumentType]

    p1 = _birth(tmp_path / ".ava-t1", tmp_path)
    bound.update(p1.values())  # t1 starts: its whole block is now listening
    p2 = _birth(tmp_path / ".ava-t2", tmp_path)

    # Ports: the two service maps must share no port numbers.
    assert set(p1.values()).isdisjoint(set(p2.values())), (
        f"port overlap: {set(p1.values()) & set(p2.values())}"
    )
    # Each cluster's own pg/redis ports are distinct — separate data-plane instances.
    assert p1["postgres"] != p2["postgres"]
    assert p1["redis"] != p2["redis"]
    # Port blocks differ by exactly BLOCK_SIZE: t1 gets base 18000, t2 the next block.
    assert min(p2.values()) - min(p1.values()) == BLOCK_SIZE


def test_birth_beside_a_stopped_cluster_may_reuse_its_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing lists the stopped cluster, so its unbound block looks free and is
    handed out again; `ava start`'s port preflight then refuses whichever of the
    two starts second (user ruling: probe at birth, refuse at start)."""
    monkeypatch.setattr(cluster, "port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]

    p1 = _birth(tmp_path / ".ava-t1", tmp_path)
    p2 = _birth(tmp_path / ".ava-t2", tmp_path)

    assert p1 == p2
