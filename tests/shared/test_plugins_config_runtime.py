"""plugins_config.load_for_runtime — dangling config entries degrade, never raise.

QA nit 2 from PR #878: `load()` stayed fail-fast at every consumer other than
`_load_extensions`. `load_for_runtime` is the shared runtime wrapper those
consumers use; strict `load()` (interactive CLI paths) keeps raising.
"""

from pathlib import Path

import pytest

from shared import paths
from shared.config import settings
from shared.plugins_config import DanglingPlugin, load, load_for_runtime, write_local


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "plugins"
    user.mkdir()
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


def test_load_for_runtime_drops_dangling_with_warning(
    loguru_records: list[dict],
) -> None:
    write_local({"plugins": {"real": {"enabled": True}, "vanished": {"enabled": False}}})

    config = load_for_runtime({"real"})  # must not raise

    assert set(config.plugins) == {"real"}
    assert config.plugins["real"].enabled
    assert any("vanished" in r["message"] for r in loguru_records)


def test_load_for_runtime_keeps_enabled_flags() -> None:
    write_local({"plugins": {"real": {"enabled": False}}})

    config = load_for_runtime({"real"})

    assert config.plugins["real"].enabled is False


def test_strict_load_still_raises_on_dangling() -> None:
    """Interactive CLI paths keep fail-fast: `ava plugins enable` on a plugin
    that is not on disk must keep its DanglingPlugin error."""
    write_local({"plugins": {"vanished": {"enabled": True}}})

    with pytest.raises(DanglingPlugin):
        load({"real"})
