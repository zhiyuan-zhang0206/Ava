"""The fixture's explicit storage override reaches real, isolated Postgres clusters."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from base.host.proc import run_bounded

_PROBE = """
import sys
from pathlib import Path
import tests.fixtures.env_bootstrap
import psycopg
from base.config import refresh_data_plane_settings, settings
from tests._containers import postgres

scratch = Path(sys.argv[1])
assert settings.data_plane.pg_throwaway_base == str(scratch)
refresh_data_plane_settings()
assert settings.data_plane.pg_throwaway_base == str(scratch)

def data_directory(url):
    with psycopg.connect(url) as conn:
        assert conn.execute('SELECT 1').fetchone() == (1,)
        return Path(conn.execute('SHOW data_directory').fetchone()[0])

with postgres() as first_url, postgres() as second_url:
    first, second = data_directory(first_url), data_directory(second_url)
    assert first.parents[1] == scratch and second.parents[1] == scratch
    assert first != second
    assert first.is_dir() and second.is_dir()
assert not first.parent.exists() and not second.parent.exists()
assert scratch.is_dir()
"""


def test_ci_throwaway_storage_reaches_separate_clusters_and_cleans_them() -> None:
    # Postgres Unix sockets need a short path, including on macOS.
    with tempfile.TemporaryDirectory(prefix="ava-ci-pg-", dir="/tmp") as directory:
        scratch = Path(directory) / "pg"
        scratch.mkdir()
        result = run_bounded(
            [sys.executable, "-c", _PROBE, str(scratch)],
            env={**os.environ, "AVA_PG_THROWAWAY_BASE": str(scratch), "TMPDIR": directory},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert list(scratch.iterdir()) == []
