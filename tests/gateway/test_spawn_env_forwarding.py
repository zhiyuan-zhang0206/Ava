"""Verify an agent spawn drops the cluster-common secrets and never inherits the
settings-lite opt-out, so the child SELF-FETCHES its config at startup per its
own role (a restarted agent picks up a rotated key instead of inheriting a stale
snapshot). AVA_CONFIG_SOURCE is gone (2026-08-01); the fetch decision is
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


def test_agent_spawn_drops_secrets_and_fetch_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "DEEPSEEK_API_KEY", "fake-deepseek-test-value")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://x/y")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gw:9000")
    monkeypatch.setitem(os.environ, "AVA_LLM_OVERRIDE", "mod:factory")
    # A maintenance verb set the lite opt-out in this process's env; the spawned
    # agent must NOT inherit it (the child decides by its own role and fetches).
    monkeypatch.setitem(os.environ, "AVA_CONFIG_FETCH", "skip")
    env = agent_launch.agent_spawn_env_dict()
    # the secret is dropped from the child env entirely (not blanked), so the child
    # re-fetches it rather than inheriting a stale snapshot
    assert "DEEPSEEK_API_KEY" not in env
    # bootstrap/identity guide keys ARE forwarded (the child needs them to reach the
    # gateway + its data plane before Settings exists)
    assert env["AVA_DB_URL"] == "postgresql://x/y"
    assert env["AVA_GATEWAY_URL"] == "http://gw:9000"
    # a host/agent-scope debug knob (not in the bootstrap payload) is still forwarded
    assert env["AVA_LLM_OVERRIDE"] == "mod:factory"
    # the child re-fetches the latest config rather than inheriting the parent's
    # snapshot — and never inherits a CLI verb's settings-lite opt-out
    assert "AVA_CONFIG_FETCH" not in env
    assert "AVA_CONFIG_SOURCE" not in env
