"""Autouse logical-backup passphrase fixture for services tests."""

from __future__ import annotations

import pytest

from base.paths import meiva_home
from services.backup.artifact import passphrase


@pytest.fixture(autouse=True)
def _backup_passphrase_pinned() -> None:
    """The suite home as a gateway birth leaves it: its logical-backup
    passphrase pinned (backups never derive a key from the cluster secret)."""
    passphrase.ensure_minted(meiva_home())
