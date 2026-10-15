"""Short-path scratch directory fixture for Unix domain sockets and process trees."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


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
