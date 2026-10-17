"""`GET /api/memory/graph` cache tests — one build per pool revision (task #4008).

The endpoint used to re-walk and re-parse the whole pool (~2k notes, ~2s) on
every request; it now builds once per git revision of the gateway checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from base.cluster.machine import MachineRoleMissing
from base.config.tests.configuration import snapshot_process_config
from base.packages.docs.notes import Note
from base.paths import meiva_home
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from tests.fixtures.gateway_config import gateway_test_client, gateway_test_process


def _memory_config(root: Path) -> GatewayConfig:
    return replace(gateway_config_from_boot(snapshot_process_config()), memory_root=root)


@pytest.mark.parametrize("serve_runner", [False, True], ids=["gateway-only", "combined"])
def test_memory_root_captures_boot_roles(serve_runner: bool) -> None:
    owner = snapshot_process_config()
    owner.set_field("machine_serve_gateway", True)
    owner.set_field("machine_serve_agent_runner", serve_runner)
    expected = meiva_home() / ("gateway/memory" if serve_runner else "memory")

    config = gateway_config_from_boot(owner)
    owner.set_field("machine_serve_agent_runner", not serve_runner)

    assert config.memory_root == expected
    assert gateway_config_from_boot(owner).memory_root != expected


def test_memory_root_rejects_missing_machine_roles() -> None:
    owner = snapshot_process_config()
    owner.set_field("machine_serve_gateway", False)
    owner.set_field("machine_serve_agent_runner", False)
    owner.set_field("machine_serve_observability_station", False)

    with pytest.raises(MachineRoleMissing):
        gateway_config_from_boot(owner)


# meiva:note
# Automatic maintenance triggered by `git commit` detaches by default (maintenance.autoDetach, falling back to gc.autoDetach) and its background process keeps the repository's object-database lock (.git/objects/maintenance.lock) after the commit command returns; a fixture whose checkout is copied right after the commit disables auto-maintenance (maintenance.auto=false, gc.auto=0) so no maintenance process exists to race the copy.
# meiva:link gateway/tests/routers/docs/memory-graph-fixture.meiva.okf.md
def _init_repo(root: Path) -> None:
    (root / "MEMORY.md").write_text("# index")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text(
        "---\ntype: Memory\nmeiva_agent: '1'\ndescription: a\ntags: [x]\n---\nbody\n"
    )
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "maintenance.auto", "false"],
        ["git", "config", "gc.auto", "0"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)  # noqa: S603


def _track_builds(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    import gateway.routers.memory as gw_memory

    calls: list[Path] = []
    real = gw_memory.walk_notes

    def counting(
        root: Path, *, skip_names: frozenset[str], warnings: list[str]
    ) -> Iterator[tuple[Path, Note]]:
        calls.append(root)
        return real(root, skip_names=skip_names, warnings=warnings)

    monkeypatch.setattr(gw_memory, "walk_notes", counting)
    return calls


def test_graph_builds_once_per_pool_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two requests on the same HEAD share one build; a new commit rebuilds."""
    # meiva:note
    # The argv is a locally built literal git command, so no shell interprets it.
    root = tmp_path / "pool"
    root.mkdir()
    _init_repo(root)
    calls = _track_builds(monkeypatch)

    with gateway_test_client(config=_memory_config(root)) as client:
        first = client.get("/api/memory/graph")
        second = client.get("/api/memory/graph")
    assert first.status_code == second.status_code == 200
    assert len(calls) == 1
    assert second.json() == first.json()

    # A pull/consolidation commit moves HEAD — the next request rebuilds.
    (root / "notes" / "a.md").write_text(
        "---\ntype: Memory\nmeiva_agent: '1'\ndescription: a\ntags: [x]\n---\nbody2\n"
    )
    for cmd in (
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "change"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)  # noqa: S603

    with gateway_test_client(config=_memory_config(root)) as client:
        third = client.get("/api/memory/graph")
    assert third.status_code == 200
    assert len(calls) == 2


def test_graph_uncached_without_a_git_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout git cannot describe (no repo) keeps the rebuild-per-request
    behavior — the cache must never serve a graph it cannot key."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "README.md").write_text("not a repo")
    calls = _track_builds(monkeypatch)

    with gateway_test_client(config=_memory_config(root)) as client:
        assert client.get("/api/memory/graph").status_code == 200
        assert client.get("/api/memory/graph").status_code == 200
    assert len(calls) == 2


def test_two_apps_keep_memory_notes_and_graphs_separate(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    _init_repo(alpha)
    shutil.copytree(alpha, beta)
    # Both checkouts have the same revision and note path; the app's configured
    # root must still determine the graph and note, including its working tree.
    (beta / "notes" / "a.md").write_text(
        "---\ntype: Memory\ntitle: Beta\ndescription: beta\ntags: [x]\n---\nbeta body\n"
    )

    with (
        gateway_test_process() as process,
        gateway_test_client(process=process, config=_memory_config(alpha)) as first,
        gateway_test_client(process=process, config=_memory_config(beta)) as second,
    ):
        assert first.app.state.memory_graph_cache is not second.app.state.memory_graph_cache
        for client, description, body in ((first, "a", "body"), (second, "beta", "beta body")):
            graph = client.get("/api/memory/graph")
            note = client.get("/api/memory/note", params={"path": "notes/a.md"})
            repeated = client.get("/api/memory/graph")
            assert graph.status_code == note.status_code == repeated.status_code == 200
            node = next(node for node in graph.json()["nodes"] if node["path"] == "notes/a.md")
            assert node["description"] == description
            assert note.json()["body"].strip() == body
            assert repeated.json() == graph.json()
