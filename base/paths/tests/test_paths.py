"""base/paths/__init__.py unit tests — AVA_HOME resolution + path helpers + first-access mkdir."""

from pathlib import Path

import pytest

from base import paths
from base.paths.home import default_home, is_default_home


@pytest.fixture(autouse=True)
def _isolate_ava_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test points AVA_HOME at tmp_path to avoid polluting ~/.ava."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "ava"))


def test_ava_home_reads_settings(tmp_path: Path):
    assert paths.ava_home() == tmp_path / "ava"


def test_ava_home_auto_mkdirs(tmp_path: Path):
    target = tmp_path / "ava"
    assert not target.exists()
    paths.ava_home()
    assert target.is_dir()


def test_ava_home_repairs_owner_only_mode(tmp_path: Path):
    """The unit root cannot retain permissions inherited from a lax umask."""
    target = tmp_path / "ava"
    target.mkdir()
    target.chmod(0o755)

    assert paths.ava_home() == target
    assert target.stat().st_mode & 0o777 == 0o700


def test_plugins_dir_under_ava_home(tmp_path: Path):
    assert paths.plugins_dir() == tmp_path / "ava" / "plugins"


def test_plugins_dir_auto_mkdirs(tmp_path: Path):
    target = tmp_path / "ava" / "plugins"
    assert not target.exists()
    paths.plugins_dir()
    assert target.is_dir()


def test_default_home_is_bare_ava():
    assert default_home() == Path("~/.ava").expanduser()


def test_is_default_home_expands_and_compares():
    assert is_default_home(Path("~/.ava"))
    assert not is_default_home(Path("~/.ava-foo"))
