"""Local fixtures for the services test package.

The ava-root skeleton needs two accommodations:

- `short_tmp`: unix socket paths are length-capped (about 100 bytes), so
  pytest's `tmp_path` — whose path is long — cannot host them.
- a collection guard: the skeleton ships its POSIX mechanisms first (flock,
  unix sockets); elsewhere its modules cannot even import yet, so its test
  files are excluded from collection there.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_AVA_ROOT_TESTS = [
    "test_ava_root_daemon.py",
    "test_ava_root_ipc.py",
    "test_ava_root_manifest.py",
    "test_ava_root_supervisor.py",
]

collect_ignore: list[str] = []
if sys.platform == "win32":
    collect_ignore += _AVA_ROOT_TESTS


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
