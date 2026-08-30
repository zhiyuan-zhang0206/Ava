"""Provider-plugin API-key delivery across bootstrap, agent spawn, and seed."""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from shared import paths
from shared.envfile import upsert_env

pytest_plugins = ("tests.shared.test_provider_plugins",)


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
    path.write_text(contents)
    upsert_env(path, {"AVA_RUNNER_DB_PASSWORD": "abc"})


def test_bootstrap_serves_an_enabled_plugin_key_from_the_env_file(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A split runner receives the raw key text from the gateway's `.env` file."""
    from shared import config

    provider_plugin()
    _write_bootstrap_env(plugin_env, "TESTP_API_KEY=sk-x\n")

    payload = config.bootstrap_config_values()
    assert payload["TESTP_API_KEY"] == "sk-x"
    valid = {config.field_alias(name) for name in config.BOOTSTRAP_FIELDS}
    from shared.env_registry import _enabled_provider_key_envs

    assert set(payload) <= valid | _enabled_provider_key_envs()


def test_bootstrap_omits_an_absent_plugin_key(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A plugin key missing from the raw `.env` file is not synthesized."""
    from shared import config

    provider_plugin()
    _write_bootstrap_env(plugin_env, "")

    assert "TESTP_API_KEY" not in config.bootstrap_config_values()


def test_bootstrap_omits_a_disabled_plugin_key(
    provider_plugin: Callable[..., None], plugin_env: Path
) -> None:
    """A file key for a disabled plugin never crosses the bootstrap boundary."""
    from shared import config

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
    from shared import config

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
    from shared import config

    provider_plugin(key_env="AVA_DB_URL")
    _write_bootstrap_env(plugin_env, "AVA_DB_URL=postgresql://ava:pw@127.0.0.1:5433/ava\n")
    monkeypatch.setattr(config, "_self_machine_host", lambda: "10.0.0.3")

    assert urlsplit(config.bootstrap_config_values()["AVA_DB_URL"]).hostname == "10.0.0.3"


def test_agent_child_env_forwards_only_enabled_plugin_keys(
    provider_plugin: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached-agent allowlist carries a declared parent key, never an arbitrary one."""
    from shared.env_registry import child_env

    monkeypatch.setenv("TESTP_API_KEY", "sk-x")
    monkeypatch.setenv("UNDECLARED_PROVIDER_KEY", "must-not-forward")

    provider_plugin()
    assert child_env("agent", "posix")["TESTP_API_KEY"] == "sk-x"
    assert "UNDECLARED_PROVIDER_KEY" not in child_env("agent", "posix")


def test_agent_child_env_excludes_a_disabled_plugin_key(
    provider_plugin: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled provider may not pass its inherited key to an agent process."""
    from shared.env_registry import child_env

    monkeypatch.setenv("TESTP_API_KEY", "sk-x")
    provider_plugin()
    (paths.ava_home() / "plugins_config.json").write_text(
        '{"plugins": {"test_provider": {"enabled": false}}}'
    )

    assert "TESTP_API_KEY" not in child_env("agent", "posix")


def test_seed_allowlist_includes_an_enabled_plugin_key(
    provider_plugin: Callable[..., None],
) -> None:
    """A fresh worktree may copy provider-plugin credentials, but no other new key."""
    from shared.env_registry import derived_env_keys, env_identity_keys, seed_allowlist

    provider_plugin()
    seed_allowlist.cache_clear()

    assert "TESTP_API_KEY" in seed_allowlist()
    assert not (seed_allowlist() & (derived_env_keys() | env_identity_keys()))


@pytest.mark.parametrize(
    "key_env",
    [
        "AVA_CLUSTER_SECRET",
        "AVA_DB_URL",
        "AVA_TELEGRAM_BOT_TOKEN",
        "AVA_RUNNER_DB_PASSWORD",
    ],
)
def test_seed_allowlist_excludes_plugin_keys_that_are_not_provider_credentials(
    provider_plugin: Callable[..., None], key_env: str
) -> None:
    """A plugin declaration cannot make a worktree seed protected credentials."""
    from shared.env_registry import derived_env_keys, env_identity_keys, seed_allowlist

    provider_plugin(key_env=key_env)
    seed_allowlist.cache_clear()

    assert key_env not in seed_allowlist()
    assert not (seed_allowlist() & (derived_env_keys() | env_identity_keys()))
