"""The fixture's explicit storage override reaches real, isolated Postgres clusters."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from base.host.proc import run_bounded

_PROBE = Path(__file__).with_name("child_throwaway_storage.py")


def test_ci_throwaway_storage_reaches_separate_clusters_and_cleans_them() -> None:
    # Postgres Unix sockets need a short path, including on macOS.
    with tempfile.TemporaryDirectory(prefix="ava-ci-pg-", dir="/tmp") as directory:
        scratch = Path(directory) / "pg"
        scratch.mkdir()
        result = run_bounded(
            [sys.executable, str(_PROBE), str(scratch)],
            env={**os.environ, "AVA_PG_THROWAWAY_BASE": str(scratch), "TMPDIR": directory},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert list(scratch.iterdir()) == []
