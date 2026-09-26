"""Private native root fixtures; CI can select this folder without database fixtures."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def native_env(tmp_path: Path) -> dict[str, str]:
    """No inherited Ava settings, credentials, or Python source redirection."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        AVA_HOME=str(home),
        AVA_HOME_OVERRIDE="1",
        AVA_CLUSTER_REGISTRY=str(tmp_path / "registry" / "clusters.json"),
        AVA_CONFIG_FETCH="skip",
        AVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
        AVA_REDIS_URL="redis://127.0.0.1:1/0",
        AVA_OS_JOBS_ENABLED="0",
        AVA_TELEMETRY_OTLP_ENABLED="0",
        AVA_BROWSER_ENABLED="0",
        AVA_MACHINE_SERVE_GATEWAY="0",
        AVA_MACHINE_SERVE_AGENT_RUNNER="1",
    )
    return env


def pytest_configure(config: pytest.Config) -> None:
    # Before any Ava import, for native CI that selects only this folder with
    # --confcutdir and so runs without the repo conftest. When the repo conftest
    # is loaded it already isolates the whole session; this hook runs before
    # every collected test, so rewriting os.environ here would leak into every
    # other directory (its home, data plane and registry) in a combined run.
    # pytest registers parent conftests first, keyed by their path.
    repo_conftest = Path(__file__).parents[2] / "conftest.py"
    if config.pluginmanager.get_plugin(str(repo_conftest)) is not None:
        return
    # Do not trust a caller's existing AVA_HOME_OVERRIDE as test isolation.
    import tempfile

    isolated = tempfile.TemporaryDirectory(prefix="ava-native-root-")
    config.add_cleanup(isolated.cleanup)
    os.environ.update(
        AVA_HOME=str(Path(isolated.name) / "home"),
        AVA_CLUSTER_REGISTRY=str(Path(isolated.name) / "registry" / "clusters.json"),
        AVA_HOME_OVERRIDE="1",
        AVA_CONFIG_FETCH="skip",
        AVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
        AVA_REDIS_URL="redis://127.0.0.1:1/0",
        AVA_OS_JOBS_ENABLED="0",
        AVA_TELEMETRY_OTLP_ENABLED="0",
    )
