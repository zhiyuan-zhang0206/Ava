"""Private native root fixtures; CI can select this folder without database fixtures."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def native_env(tmp_path: Path) -> dict[str, str]:
    """No inherited Meiva settings, credentials, or Python source redirection."""
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
        MEIVA_HOME=str(home),
        MEIVA_CONFIG_FETCH="skip",
        MEIVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
        MEIVA_REDIS_URL="redis://127.0.0.1:1/0",
        MEIVA_OS_JOBS_ENABLED="0",
        MEIVA_TELEMETRY_OTLP_ENABLED="0",
        MEIVA_BROWSER_ENABLED="0",
        MEIVA_MACHINE_SERVE_GATEWAY="0",
        MEIVA_MACHINE_SERVE_AGENT_RUNNER="1",
    )
    return env


def pytest_configure(config: pytest.Config) -> None:
    # Before any Meiva import, for native CI that selects only this folder with
    # --confcutdir and so runs without the repo-root conftest. When that
    # conftest is loaded it already isolates the whole session; this hook runs
    # before every collected test, so rewriting os.environ here would leak into
    # every other directory (its home and data plane) in a combined
    # run. pytest registers parent conftests first, keyed by their path.
    repo_conftest = Path(__file__).parents[4] / "conftest.py"
    if config.pluginmanager.get_plugin(str(repo_conftest)) is not None:
        return
    # Do not trust a caller's existing MEIVA_HOME as test isolation: name a home of our own.
    import tempfile

    isolated = tempfile.TemporaryDirectory(prefix="meiva-native-root-")
    config.add_cleanup(isolated.cleanup)
    os.environ.update(
        MEIVA_HOME=str(Path(isolated.name) / "home"),
        MEIVA_CONFIG_FETCH="skip",
        MEIVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
        MEIVA_REDIS_URL="redis://127.0.0.1:1/0",
        MEIVA_OS_JOBS_ENABLED="0",
        MEIVA_TELEMETRY_OTLP_ENABLED="0",
    )
