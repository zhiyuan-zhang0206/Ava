"""mmap-backed shared memory pinning for every PG Ava starts (Task #1263).

`pg_shm_args` feeds the `pg_ctl -o` string of both startup paths — the
per-cluster data plane (`cli/commands/_cluster_instance.py`) and the throwaway
test/eval clusters (`throwaway_postgres`). The two settings move Postgres' main
shared memory region and its dynamic segments out of POSIX shm (/dev/shm on
Linux) into files under the data directory, so an external unlink of /dev/shm
cannot take a running instance down — the staging incident that motivated the
task (the machine-side fix was the same two settings).
"""

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
