import pytest

from agent.hooks import BUILTIN_PLUGINS
from base.packages.plugins.enable_config import parse_description
from base.paths import repo_plugins_dir


def test_builtin_plugins_is_tuple_of_str():
    assert isinstance(BUILTIN_PLUGINS, tuple)
    assert all(isinstance(n, str) for n in BUILTIN_PLUGINS)
    assert BUILTIN_PLUGINS, "at least one built-in plugin expected"


@pytest.mark.parametrize("name", BUILTIN_PLUGINS)
def test_known_plugin_has_entry_point(name: str):
    plugin_py = repo_plugins_dir() / name / "plugin.py"
    assert plugin_py.exists(), f"plugins/{name}/plugin.py missing"


@pytest.mark.parametrize("name", BUILTIN_PLUGINS)
def test_each_plugin_has_description(name: str):
    plugin_py = repo_plugins_dir() / name / "plugin.py"
    desc = parse_description(plugin_py)
    assert desc, f"plugins/{name}/plugin.py lacks __description__"
