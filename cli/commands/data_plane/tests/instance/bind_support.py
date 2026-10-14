"""Fixtures the cluster-instance bind tests share: the native effects they never run."""

import os
import subprocess
from collections.abc import Generator
from unittest.mock import patch

import pytest

from base.config import ConfigBoot, settings
from cli.commands.data_plane import cluster_instance as _ci


@pytest.fixture(autouse=True)
def config_boot_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Restore process delivery from each independent operation's boot."""

    def build_owner() -> ConfigBoot:
        config = ConfigBoot()
        config.view.walg.walg_config_file = settings.walg.walg_config_file
        return config

    monkeypatch.setattr(_ci, "ConfigBoot", build_owner)
    with patch.dict(os.environ):
        yield


def no_native_effect(*_args: object, **_kwargs: object) -> None:
    pass


@pytest.fixture(autouse=True)
def native_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ci.port_preflight, "require_listener", no_native_effect)
    monkeypatch.setattr(_ci.ownership, "require_postgres", no_native_effect)
    # The loaded-hba proof dials the postmaster; real Postgres covers it
    # (cli/commands/tests/test_single_box.py).
    monkeypatch.setattr(_ci, "require_authenticated_hba", no_native_effect)


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []
