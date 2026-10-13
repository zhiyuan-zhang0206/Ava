"""Child process: two throwaway Postgres clusters land under the explicit storage override.

Usage: `child_throwaway_storage.py <scratch directory>`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import psycopg

import tests.fixtures.env_bootstrap  # noqa: F401  # pyright: ignore[reportUnusedImport]
from base.cluster.machine.boot import machine_from_boot
from base.config import refresh_data_plane_settings, settings
from base.config.tests.configuration import snapshot_process_config
from tests._containers import postgres

scratch = Path(sys.argv[1])
assert settings.data_plane.pg_throwaway_base == str(scratch)
machine = machine_from_boot(snapshot_process_config())
refresh_data_plane_settings()
assert settings.data_plane.pg_throwaway_base == str(scratch)


def data_directory(url: str) -> Path:
    with psycopg.connect(url) as conn:
        row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)
        row = conn.execute("SHOW data_directory").fetchone()
        assert row is not None
        return Path(row[0])


with (
    postgres(machine_name=machine.name) as first_url,
    postgres(machine_name=machine.name) as second_url,
):
    first, second = data_directory(first_url), data_directory(second_url)
    assert first.parents[1] == scratch and second.parents[1] == scratch
    assert first != second
    assert first.is_dir() and second.is_dir()
assert not first.parent.exists() and not second.parent.exists()
assert scratch.is_dir()
