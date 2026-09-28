"""Full-stop helper extras use the shared exact-home retirement boundary."""

from pathlib import Path

import pytest

from cli.commands._stop_extras import stop_permissions_helper


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".ava"
    home.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    return home


def test_helper_macos_stop_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", True)

    def failed(target: Path, *, helper_port: int, force: bool, timeout_s: float) -> None:
        assert target == home and helper_port > 0
        raise RuntimeError("job survived")

    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", failed)
    with pytest.raises(RuntimeError, match="job survived"):
        stop_permissions_helper()


def test_helper_non_macos_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.platform.IS_MACOS", False)

    def unexpected(target: Path, *, helper_port: int, force: bool, timeout_s: float) -> None:
        pytest.fail("a user-wide Windows helper must not be stopped by one home")

    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", unexpected)
    stop_permissions_helper()
