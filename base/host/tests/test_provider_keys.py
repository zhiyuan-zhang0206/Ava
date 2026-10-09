"""Provider-plugin API-key delivery across bootstrap and agent spawn."""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from base import paths
from base.host.env.dotenv_file import upsert_env

pytest_plugins = ("base.lm.tests.providers.test_provider_plugins",)


@pytest.fixture
def plugin_env() -> Generator[Path, None, None]:
    """Temporarily replace the session test home's provider-key `.env` file."""
    path = paths.ava_home() / ".env"
    original = path.read_bytes() if path.exists() else None
    yield path
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def _write_bootstrap_env(path: Path, contents: str) -> None:
    """Write a plugin fixture with the runner credential bootstrap requires."""
    from base import config

    path.write_text(contents)
    upsert_env(path, {"AVA_RUNNER_DB_PASSWORD": "abc"})
    if "AVA_DB_URL=" not in contents:
        upsert_env(path, {"AVA_DB_URL": str(config.settings.data_plane.db_url)})


def test_bootstrap_serves_an_enabled_plugin_key_from_the_env_file(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A split runner receives the raw key text from the gateway's `.env` file."""
    from base import config
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    provider_plugin()
    _write_bootstrap_env(plugin_env, "TESTP_API_KEY=sk-x\n")

    payload = config.bootstrap_config_values()
    assert payload["TESTP_API_KEY"] == "sk-x"
    valid = {config.field_alias(name) for name in config.BOOTSTRAP_FIELDS}
    assert set(payload) <= valid | {"TESTP_API_KEY", PLUGIN_CLUSTER_CONFIG_ENV}


def test_bootstrap_omits_an_absent_plugin_key(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A plugin key missing from the raw `.env` file is not synthesized."""
    from base import config

    provider_plugin()
    _write_bootstrap_env(plugin_env, "")

    assert "TESTP_API_KEY" not in config.bootstrap_config_values()


def test_bootstrap_omits_a_disabled_plugin_key(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A file key for a disabled plugin never crosses the bootstrap boundary."""
    from base import config

    provider_plugin()
    _write_bootstrap_env(plugin_env, "TESTP_API_KEY=sk-x\n")
    (paths.ava_home() / "plugins_config.json").write_text(
        '{"plugins": {"test_provider": {"enabled": false}}}'
    )

    assert "TESTP_API_KEY" not in config.bootstrap_config_values()


def test_bootstrap_serves_a_duplicate_plugin_key_once(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """Two bindings may share a key without duplicating or rejecting bootstrap."""
    from base import config

    provider_plugin()
    provider_plugin(prefix="testq-", model="testq-1", dir_name="test_provider_q")
    _write_bootstrap_env(plugin_env, "TESTP_API_KEY=sk-x\n")

    payload = config.bootstrap_config_values()
    assert payload["TESTP_API_KEY"] == "sk-x"
    assert list(payload).count("TESTP_API_KEY") == 1


def test_bootstrap_keeps_modeled_alias_after_reachable_host_rewrite(
    provider_plugin: Callable[..., None], plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin key matching a Settings alias cannot undo its bootstrap transform."""
    from base import config

    provider_plugin(key_env="AVA_DB_URL")
    _write_bootstrap_env(plugin_env, "AVA_DB_URL=postgresql://ava:pw@127.0.0.1:5433/ava\n")
    monkeypatch.setattr(
        "base.config.domains.storage.data_plane.self_machine_host", lambda: "10.0.0.3"
    )

    assert urlsplit(config.bootstrap_config_values()["AVA_DB_URL"]).hostname == "10.0.0.3"
