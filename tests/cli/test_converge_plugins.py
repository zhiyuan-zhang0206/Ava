"""run_plugin_scaffolds with a dangling config entry — fail-soft (QA nit 2, #878).

Converge runs on every `ava start` / `ava cluster update`; a config entry
whose plugin directory is gone (interrupted upgrade, manual rm) must not
block the cluster from starting.
"""

from pathlib import Path

import pytest

from cli.commands._converge_plugins import run_plugin_scaffolds
from shared import paths
from shared.config import settings
from shared.plugins_config import write_local


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


def test_scaffold_runs_despite_dangling_config() -> None:
    pdir = paths.plugins_dir() / "real"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (pdir / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold():\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('1')\n",
        encoding="utf-8",
    )
    write_local({"plugins": {"real": {"enabled": True}, "vanished": {"enabled": True}}})

    result = run_plugin_scaffolds()  # must not raise

    assert result.ran == ["real"]
    assert (pdir / "scaffolded.marker").exists()
