"""Fixtures for the services tests (registered by `tests/fixtures/path_scopes.py`).

The ava-root skeleton needs one accommodation: `short_tmp`. Unix socket paths are
length-capped (about 100 bytes), so pytest's `tmp_path` — whose path is long —
cannot host them.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _backup_passphrase_pinned() -> None:
    """The suite home as a gateway birth leaves it: its logical-backup
    passphrase pinned (backups never derive a key from the cluster secret)."""
    from base.paths import ava_home
    from services.backup.artifact import passphrase

    passphrase.ensure_minted(ava_home())


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short-path scratch directory for unix sockets and process trees."""
    # `/tmp` keeps the socket path short on the POSIX platforms this suite runs
    # on; the system temp dir (e.g. a per-user sandbox) can be far longer.
    directory = Path(tempfile.mkdtemp(prefix="ava-root-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
