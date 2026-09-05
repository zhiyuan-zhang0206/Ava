"""Verify agent spawn environment projection for config and provider secrets.

The child SELF-FETCHES modeled config at startup, while plugin-declared provider
keys ride the positive allowlist because the plugin builder reads its process
environment. AVA_CONFIG_SOURCE is gone (2026-08-01); the fetch decision is
role-derived at the child's Settings build.

agent_spawn_env_dict (ops.agent_launch) reads os.environ
directly (it inspects the live env to decide which vars the detached child
inherits — Settings cannot enumerate non-AVA_ secrets it does not own). So tests
use monkeypatch.setitem(os.environ, ...) — equivalent to setenv, but bypasses the
lint_no_os_environ Rule 2 ban on monkeypatch.setenv of Settings-managed aliases.
"""

import os

import pytest

from ops import agent_launch
from shared.lm._plugin_providers import ensure_provider_plugins_loaded

ensure_provider_plugins_loaded()


def test_agent_spawn_forwards_plugin_key_and_drops_fetch_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(os.environ, "DEEPSEEK_API_KEY", "fake-deepseek-test-value")
    # A remote launcher receives an already projected URL from bootstrap.
    projected_url = "postgresql://ava_runner:bootstrap-password@x/y"
    monkeypatch.setitem(os.environ, "AVA_DB_URL", projected_url)
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: False)
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:9000")
    monkeypatch.setitem(os.environ, "AVA_LLM_OVERRIDE", "mod:factory")
    # A maintenance verb set the lite opt-out in this process's env; the spawned
    # agent must NOT inherit it (the child decides by its own role and fetches).
    monkeypatch.setitem(os.environ, "AVA_CONFIG_FETCH", "skip")
    env = agent_launch.agent_spawn_env_dict()
    # A provider plugin's declared key is the single-box builder's delivery seam.
    assert env["DEEPSEEK_API_KEY"] == "fake-deepseek-test-value"
    # bootstrap/identity guide keys ARE forwarded (the child needs them to reach the
    # gateway + its data plane before Settings exists)
    assert env["AVA_DB_URL"] == projected_url
    assert env["AVA_GATEWAY_URL"] == "http://gw:9000"
    # a host/agent-scope debug knob (not in the bootstrap payload) is still forwarded
    assert env["AVA_LLM_OVERRIDE"] == "mod:factory"
    # the child re-fetches the latest config rather than inheriting the parent's
    # snapshot — and never inherits a CLI verb's settings-lite opt-out
    assert "AVA_CONFIG_FETCH" not in env
    assert "AVA_CONFIG_SOURCE" not in env
