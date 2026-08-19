"""mmap-backed shared memory pinning for every PG Ava starts (Task #1263), and
the Postgres session timezone pin (tz audit PR-1).

`pg_shm_args` / `pg_tz_args` feed the `pg_ctl -o` string of both startup paths
— the per-cluster data plane (`cli/commands/_cluster_instance.py`) and the
throwaway test/eval clusters (`throwaway_postgres`). `pg_shm_args`' two
settings move Postgres' main shared memory region and its dynamic segments out
of POSIX shm (/dev/shm on Linux) into files under the data directory, so an
external unlink of /dev/shm cannot take a running instance down — the staging
incident that motivated the task (the machine-side fix was the same two
settings). `pg_tz_args` pins the session timezone to UTC so psycopg3 returns
every timestamptz as a stable `+00:00`-offset datetime instead of one that
drifts with the host OS timezone.
"""

import psycopg
import pytest

from shared import pg_tools

_MMAP_ARGS = "-c shared_memory_type=mmap -c dynamic_shared_memory_type=mmap"


def test_pg_shm_args_linux_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux/WSL — the incident platform: the pin is unconditional there."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: False)
    assert pg_tools.pg_shm_args() == _MMAP_ARGS


def test_pg_shm_args_macos_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS — compatibility verified 2026-08-13: the vendored PG 17.4 starts
    with both settings and they take effect (pg_settings, source=command line);
    mmap is already the macOS default for the main region and DSM mmap has been
    supported since PG 15. So macOS pins them too."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    assert pg_tools.pg_shm_args() == _MMAP_ARGS


def test_pg_shm_args_macos_incompatibility_keeps_status_quo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task's named fallback: a future macOS PG build that rejects either
    option flips `_PG_SHM_MMAP_OK_ON_MACOS` to False — macOS then keeps its
    status quo (no explicit settings) while Linux/WSL keeps the pin."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    monkeypatch.setattr(pg_tools, "_PG_SHM_MMAP_OK_ON_MACOS", False)
    assert pg_tools.pg_shm_args() == ""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: False)
    assert pg_tools.pg_shm_args() == _MMAP_ARGS


def test_pg_tz_args_is_unconditional_utc() -> None:
    """Unlike `pg_shm_args`, there is no platform branch: every PG this
    codebase starts pins the session timezone to UTC."""
    assert pg_tools.pg_tz_args() == "-c timezone=UTC"


def test_throwaway_pg_session_timezone_is_utc(db_conn: psycopg.Connection) -> None:
    """The live throwaway pg the test suite runs against (started via
    `throwaway_postgres`, not `_cluster_instance.py`) actually carries the
    pin — this is the seam PR-1's behavior-change test lock depends on:
    without it, `.isoformat()` on a value read back through `db_conn` would
    carry the CI/dev host's OS timezone offset instead of a stable `+00:00`."""
    with db_conn.cursor() as cur:
        cur.execute("SHOW timezone")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "UTC"
