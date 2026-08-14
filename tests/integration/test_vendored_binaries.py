"""Vendored relocatable Postgres: resolution + a real download/extract.

`test_pg_tool_prefers_vendored_dir` is a fast unit check (no network). The real
download test fetches the pinned zonky jar from Maven Central, extracts it, and
runs the relocatable `initdb` standalone -- the bring-up `ava start` depends on for
a brew-free machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared import pg_tools
from shared import runtime_binaries as rb
from shared.config import settings


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the host-level runtime root at a tmp dir (via the cluster-registry
    anchor), so a test never touches the real ~/.ava/runtime."""
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))


def test_pg_tool_prefers_vendored_dir(isolated_runtime: None) -> None:
    # Absent vendored tree -> pg_tool falls back to the host (brew/apt) path.
    assert "runtime" not in str(pg_tools.pg_tool("initdb"))

    # A present vendored tree wins.
    bin_dir = rb.vendored_pg_dir() / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "initdb").write_text("#!/bin/sh\n")
    assert pg_tools.pg_tool("initdb") == bin_dir / "initdb"
    assert rb.vendored_pg_bin_dir() == bin_dir


def test_ensure_pg_binaries_real_download(isolated_runtime: None) -> None:
    assert rb.vendored_pg_bin_dir() is None

    bin_dir = rb.ensure_pg_binaries()
    assert (bin_dir / "initdb").exists()
    assert rb.vendored_pg_bin_dir() == bin_dir

    # The relocatable binary runs standalone (no brew, from its extracted path).
    out = subprocess.run(  # noqa: S603 — resolved vendored initdb path + static flag
        [str(bin_dir / "initdb"), "--version"], capture_output=True, text=True, check=True
    )
    assert "17.4" in out.stdout

    # Idempotent: a second ensure is a no-op returning the same dir (no re-download).
    assert rb.ensure_pg_binaries() == bin_dir
