"""`GET /api/memory/pool` endpoint unit tests — consolidated pool git bundle
for split-runner memory bootstrap (2026-08-27 company-mini incident)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def _init_repo(root: Path) -> str:
    """Real git repo with a note tree; returns its HEAD sha."""
    (root / "MEMORY.md").write_text("# index")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("a")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)  # noqa: S603
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_pool_serves_bundle_of_consolidated_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The endpoint serves a git bundle of the gateway checkout's HEAD: the
    tracked tree rides in with real ancestry; untracked machine-local paths
    (.cache/.githooks/__pycache__/.DS_Store) never do."""
    import gateway.routers.memory as _gw_memory

    root = tmp_path / "pool"
    root.mkdir()
    head = _init_repo(root)
    # untracked machine-local clutter
    (root / ".cache").mkdir()
    (root / ".cache" / "x").write_text("x")
    (root / ".githooks").mkdir()
    (root / ".githooks" / "pre-commit").write_text("x")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "c.pyc").write_bytes(b"x")
    (root / ".DS_Store").write_text("x")

    monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: root)

    with TestClient(app) as client:
        resp = client.get("/api/memory/pool")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["X-Pool-Head"] == head

    # write the served bundle to a file, clone it for real, assert the tree
    clone = tmp_path / "clone"
    bundle_file = tmp_path / "pool.bundle"
    bundle_file.write_bytes(resp.content)
    subprocess.run(  # noqa: S603
        ["git", "clone", "-q", str(bundle_file), str(clone)],
        check=True,
        capture_output=True,
    )
    assert (clone / "MEMORY.md").read_text() == "# index"
    assert (clone / "notes" / "a.md").read_text() == "a"
    assert not (clone / ".cache").exists()
    assert not (clone / ".githooks").exists()
    assert not (clone / "__pycache__").exists()
    assert not (clone / ".DS_Store").exists()


def test_pool_404_when_gateway_checkout_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway without an initialized consolidated checkout answers 404 —
    the runner's bootstrap then fails loud, it never installs garbage."""
    import gateway.routers.memory as _gw_memory

    monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path / "nope")

    with TestClient(app) as client:
        resp = client.get("/api/memory/pool")

    assert resp.status_code == 404
