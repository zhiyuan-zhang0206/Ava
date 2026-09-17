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

`pg_start_env` (Task #3754) is the third startup-path fragment: the child
environment for a server start, supplying the macOS locale fallback the
postmaster needs when the caller's environment carries none (a launchd job or
a non-interactive ssh session).
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from shared import pg_throwaway_base, pg_tools
from shared.config import settings

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


@pytest.fixture
def scratch_pg_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short scratch throwaway base for tests that really start a server.

    The Postgres socket path (`.s.PGSQL.<port>`) is capped at 103 bytes, so the
    instance root must be short; teardown force-stops and deletes anything left
    under it."""
    root = Path(tempfile.mkdtemp(prefix="ava-pg-env-", dir="/tmp"))
    monkeypatch.setattr(pg_throwaway_base, "_tmpfs_base", str(root))
    monkeypatch.setattr(pg_throwaway_base, "disk_fallback_base", lambda: root)
    monkeypatch.setattr(settings.data_plane, "pg_throwaway_base", "")
    yield root
    for data in root.glob("ava-pg-*/data"):
        if (data / "PG_VERSION").is_file():
            subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + this test's own dir
                [pg_tools.pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
    shutil.rmtree(root, ignore_errors=True)


def test_pg_start_env_macos_fills_lc_all_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #3754: launchd and non-interactive ssh start PG with no locale at
    all, and the Homebrew postmaster then aborts ("postmaster became
    multithreaded during startup") — so the start env supplies one. Verified
    live on macOS 2026-09-17 against postgresql@17 (17.11)."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    for name in ("LC_ALL", "LANG", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)

    env = pg_tools.pg_start_env()

    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["PATH"] == os.environ["PATH"], "the caller's env is inherited otherwise"


def test_pg_start_env_macos_keeps_a_caller_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that expresses a locale keeps it verbatim — the fallback never
    overrides a choice (LC_ALL directly, or LANG when LC_ALL is unset)."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert pg_tools.pg_start_env()["LC_ALL"] == "de_DE.UTF-8"

    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert "LC_ALL" not in pg_tools.pg_start_env()


def test_pg_start_env_macos_lc_ctype_alone_is_not_a_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LC_CTYPE alone (an ssh session forwarding only LC_CTYPE) does not keep
    the postmaster off the CoreFoundation path — verified: it still aborts, so
    LC_CTYPE must not suppress the fallback."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setenv("LC_CTYPE", "UTF-8")

    assert pg_tools.pg_start_env()["LC_ALL"] == "en_US.UTF-8"


def test_pg_start_env_macos_empty_value_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setlocale ignores an empty LC_ALL (verified: the postmaster still
    aborts), so the fallback treats it exactly like unset."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setenv("LC_ALL", "")

    assert pg_tools.pg_start_env()["LC_ALL"] == "en_US.UTF-8"


def test_pg_start_env_non_macos_returns_the_caller_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux needs nothing: an absent locale resolves to C without threads, and
    a minimal image may not have en_US.UTF-8 generated."""
    monkeypatch.setattr(pg_tools, "is_macos", lambda: False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)

    assert pg_tools.pg_start_env() == dict(os.environ)


def test_throwaway_pg_ctl_start_is_handed_the_built_start_env(
    scratch_pg_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #3754: the fixture's `pg_ctl start` receives pg_start_env() — the
    postmaster environment is built explicitly instead of inherited from
    whatever process runs the suite."""
    sentinel = {**os.environ, "AVA_TEST_PG_START_ENV": "1"}
    monkeypatch.setattr(pg_tools, "pg_start_env", lambda: sentinel)
    start_envs: list[object] = []
    real_run = pg_tools.subprocess.run

    def spy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if "start" in cmd and "pg_ctl" in str(cmd[0]):
            start_envs.append(kwargs.get("env"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(pg_tools.subprocess, "run", spy)

    with pg_tools.throwaway_postgres() as url, psycopg.connect(url) as conn:
        assert conn.execute("select 1").fetchone() == (1,)

    assert start_envs == [sentinel]
