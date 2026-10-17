from pathlib import Path

import pytest

from base import paths
from base.host.permissions_helper.client import HelperAtSocket, PermissionsHelperClient
from base.native_process import os_platform
from cli.commands.lifecycle._stop_extras import stop_permissions_helper
from services.desktop.permissions_helper import launchd_job


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".meiva"
    home.mkdir()
    monkeypatch.setattr(paths, "meiva_home", lambda: home)
    return home


def test_helper_macos_stop_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(os_platform, "is_macos", lambda: True)

    def failed(
        target: Path, *, helper_port: int, connect: HelperAtSocket, force: bool, timeout_s: float
    ) -> None:
        assert target == home and helper_port > 0
        assert connect is PermissionsHelperClient
        raise RuntimeError("job survived")

    monkeypatch.setattr(launchd_job, "unregister_helper", failed)
    with pytest.raises(RuntimeError, match="job survived"):
        stop_permissions_helper()


def test_helper_non_macos_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(os_platform, "is_macos", lambda: False)

    def unexpected(
        target: Path, *, helper_port: int, connect: HelperAtSocket, force: bool, timeout_s: float
    ) -> None:
        pytest.fail("the permissions helper must not be stopped on a non-macOS host")

    monkeypatch.setattr(launchd_job, "unregister_helper", unexpected)
    stop_permissions_helper()
