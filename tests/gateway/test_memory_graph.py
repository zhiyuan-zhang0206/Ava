"""`GET /api/memory/graph` cache tests — one build per pool revision (task #4008).

The endpoint used to re-walk and re-parse the whole pool (~2k notes, ~2s) on
every request; it now builds once per git revision of the gateway checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def _init_repo(root: Path) -> None:
    (root / "MEMORY.md").write_text("# index")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text(
        "---\ntype: Memory\nava_agent: '1'\ndescription: a\ntags: [x]\n---\nbody\n"
    )
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)  # noqa: S603


def _track_builds(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    import gateway.routers.memory as gw_memory

    calls: list[Path] = []
    real = gw_memory._build_memory_graph

    def counting(root: Path):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(gw_memory, "_build_memory_graph", counting)
    return calls


def test_graph_builds_once_per_pool_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two requests on the same HEAD share one build; a new commit rebuilds."""
    import gateway.routers.memory as gw_memory

    root = tmp_path / "pool"
    root.mkdir()
    _init_repo(root)
    monkeypatch.setattr(gw_memory, "gateway_memory_dir", lambda: root)
    calls = _track_builds(monkeypatch)

    with TestClient(app) as client:
        first = client.get("/api/memory/graph")
        second = client.get("/api/memory/graph")
    assert first.status_code == second.status_code == 200
    assert len(calls) == 1
    assert second.json() == first.json()

    # A pull/consolidation commit moves HEAD — the next request rebuilds.
    (root / "notes" / "a.md").write_text(
        "---\ntype: Memory\nava_agent: '1'\ndescription: a\ntags: [x]\n---\nbody2\n"
    )
    for cmd in (
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "change"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)  # noqa: S603

    with TestClient(app) as client:
        third = client.get("/api/memory/graph")
    assert third.status_code == 200
    assert len(calls) == 2


def test_graph_uncached_without_a_git_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout git cannot describe (no repo) keeps the rebuild-per-request
    behavior — the cache must never serve a graph it cannot key."""
    import gateway.routers.memory as gw_memory

    root = tmp_path / "plain"
    root.mkdir()
    (root / "README.md").write_text("not a repo")
    monkeypatch.setattr(gw_memory, "gateway_memory_dir", lambda: root)
    calls = _track_builds(monkeypatch)

    with TestClient(app) as client:
        assert client.get("/api/memory/graph").status_code == 200
        assert client.get("/api/memory/graph").status_code == 200
    assert len(calls) == 2
