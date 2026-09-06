from pathlib import Path

import pytest

from shared import maintenance, paths, pause_owner


def test_admission_read_does_not_create_an_uninstalled_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "uninstalled"
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    assert pause_owner.read().status == "inactive"
    maintenance.require_start_allowed()
    assert not home.exists()
