"""Detached agent child env (ops.agent_launch.agent_spawn_env_dict).

The child env is a POSITIVE allowlist (child_env("agent", ...)) — a non-modeled
knob (AVA_AGENT_ID, ...) never rides; cluster-scope secrets are dropped so the
child self-fetches at boot; the ambient display passthroughs ride through
(non-empty only) so the agent computes the same display verdict as the spawner.
"""

from __future__ import annotations

import os

import pytest

from ops import agent_launch

_RUNNER_URL = "postgresql://ava_runner:runner-password@x/db"


def test_agent_spawn_forwards_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent process gates its chrome MCP on the same display verdict, so the
    detached-child env dict carries $DISPLAY / $WAYLAND_DISPLAY through; $HOME
    rides too (2026-08-06: agents lost HOME in the allowlist refactor and gh
    flipped to "not logged in" — macOS bash 3.2 never restores it)."""
    # This is an enrolled runner projection; do not inspect the synthetic HOME.
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: False)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": _RUNNER_URL,
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "HOME": "/Users/op",
        },
    )
    env = agent_launch.agent_spawn_env_dict()
    assert env["DISPLAY"] == ":0"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["HOME"] == "/Users/op"


def test_agent_spawn_does_not_forward_empty_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty $DISPLAY means 'no display'; the child inherits the env wholesale,
    so an empty passthrough is dropped rather than carried through as "" (which
    would re-enable the chrome MCP gate on a headless box via a stripped-but-
    present-looking $DISPLAY)."""
    monkeypatch.setattr(os, "environ", {"AVA_DB_URL": _RUNNER_URL, "DISPLAY": ""})
    env = agent_launch.agent_spawn_env_dict()
    assert "DISPLAY" not in env


def test_agent_spawn_drops_secrets_and_fetch_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": _RUNNER_URL,
            "AVA_GATEWAY_PORT": "9000",  # guide key (single-box localhost fallback)
            "AVA_LLM_OVERRIDE": "mod:fac",  # agent-scope, not in bootstrap payload
            "AVA_RESTARTER_HEALTH_PORT": "8102",  # host-scope daemon port — forwarded
            "DEEPSEEK_API_KEY": "secret",  # cluster-common secret — must be dropped
            "AVA_CONFIG_FETCH": "skip",  # a maintenance verb's lite opt-out
        },
    )
    env = agent_launch.agent_spawn_env_dict()
    assert env["AVA_DB_URL"] == _RUNNER_URL
    assert env["AVA_GATEWAY_PORT"] == "9000"
    assert env["AVA_LLM_OVERRIDE"] == "mod:fac"
    assert "DEEPSEEK_API_KEY" not in env  # dropped so the child re-fetches
    # A daemon health port rides along, because the drop set IS "what the gateway
    # redistributes" and a health port is a per-UNIT fact the gateway does not own
    # (issue #977). Dropping it would leave a child on a unit with a non-default
    # block resolving the shared defaults for the endpoints it dials — the
    # memory-indexer's /search among them.
    assert env["AVA_RESTARTER_HEALTH_PORT"] == "8102"
    # No config-source pin (AVA_CONFIG_SOURCE is gone): the child derives it from
    # its own role at Settings build. The lite opt-out is never inherited either —
    # a spawned agent must fetch per its role, not skip because a CLI verb did.
    assert "AVA_CONFIG_FETCH" not in env
    assert "AVA_CONFIG_SOURCE" not in env


def test_agent_spawn_replaces_gateway_owner_url_with_runner_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher is the final privilege boundary: an agent receives the
    runner URL even when its gateway parent carries the owner URL, and never
    receives either data-plane admin password."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": "postgresql://ava:owner-password@127.0.0.1:5433/ava",
            "AVA_DB_ADMIN_PASSWORD": "db-admin-only",
            "AVA_REDIS_ADMIN_PASSWORD": "redis-admin-only",
            "AVA_REDIS_PASSWORD": "redis-runtime-only",
        },
    )
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: True)
    monkeypatch.setattr(
        "shared.runtime_config.read_env_aliases",
        lambda: {
            "AVA_DB_URL": "postgresql://ava:owner-password@127.0.0.1:5433/ava",
            "AVA_RUNNER_DB_PASSWORD": "runner-password",
        },
    )

    env = agent_launch.agent_spawn_env_dict()

    assert env["AVA_DB_URL"] == "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_REDIS_ADMIN_PASSWORD" not in env
    assert "AVA_REDIS_PASSWORD" not in env


def test_agent_spawn_never_pins_a_config_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child derives its config source from its own role at Settings build
    (shared.bootstrap.config_source_is_local); no pin rides in the env. A
    gateway-capable child reads the shared .env; a pure-runner child fetches."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": _RUNNER_URL,
            "DEEPSEEK_API_KEY": "secret",
            "AVA_CONFIG_FETCH": "skip",
        },
    )
    env = agent_launch.agent_spawn_env_dict()
    assert "AVA_CONFIG_SOURCE" not in env
    assert "AVA_CONFIG_FETCH" not in env
    assert "DEEPSEEK_API_KEY" not in env  # still dropped — .env / the fetch fills it


def test_agent_spawn_allowlist_never_carries_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-s3-4's headline leak: per-agent identity never rides the child env. The
    launcher stamps AVA_AGENT_ID at spawn (ops/agents.py), never inherits it."""
    monkeypatch.setattr(
        os,
        "environ",
        {"AVA_AGENT_ID": "9999", "AVA_HOME": "/tmp/h"},  # noqa: S108 — literal env values, never opened
    )
    env = agent_launch.agent_spawn_env_dict()
    assert "AVA_AGENT_ID" not in env


def test_agent_spawn_carries_temp_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """TMPDIR/TEMP/TMP are not Settings aliases, so the allowlist never carries
    them — but a child without TMPDIR falls back to the OS default temp root and
    any tool that pins a boot file to $TMPDIR drifts (v0.1.34: TMPDIR vanished
    fleet-wide, watcher boot files landed in the wrong temp root). Copied from
    os.environ, non-empty only."""
    monkeypatch.setattr(
        os,
        "environ",
        {"AVA_DB_URL": _RUNNER_URL, "TMPDIR": "/tmp/agent-tmp"},  # noqa: S108 — literal env values, never opened
    )
    env = agent_launch.agent_spawn_env_dict()
    assert env["TMPDIR"] == "/tmp/agent-tmp"  # noqa: S108 — literal, never opened


def test_agent_spawn_carries_windows_system_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the env dict replaces the child env wholesale (winproc, no
    login shell to rebuild it), so an agent child without SYSTEMROOT/WINDIR/etc.
    dies in winsock init before its first import (`import _overlapped`,
    WinError 10106 — 2026-08-07 win boot-revive launch failure, agent 2197).
    Copied from os.environ, non-empty only; POSIX needs none of them (the child
    runs under a login shell whose profile rebuilds the full environment)."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": _RUNNER_URL,
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
        },
    )
    # POSIX: system keys not carried.
    env = agent_launch.agent_spawn_env_dict()
    assert "SYSTEMROOT" not in env
    # Windows: system keys ride.
    monkeypatch.setattr(agent_launch, "IS_WINDOWS", True)
    env = agent_launch.agent_spawn_env_dict()
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["WINDIR"] == r"C:\Windows"


def test_agent_spawn_windows_sets_utf8_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows Python defaults text-mode pipes to the ANSI code page (cp1252),
    so subprocess input with CJK crashes ('charmap' codec — win agent 2197,
    2026-08-07). PYTHONUTF8=1 in the child env forces UTF-8 everywhere, the
    same default Python 3.15 moves to; POSIX already runs UTF-8 via locale."""
    monkeypatch.setattr(os, "environ", {"AVA_DB_URL": _RUNNER_URL})
    # POSIX: not set — the locale already provides UTF-8.
    env = agent_launch.agent_spawn_env_dict()
    assert "PYTHONUTF8" not in env
    # Windows: set unconditionally.
    monkeypatch.setattr(agent_launch, "IS_WINDOWS", True)
    env = agent_launch.agent_spawn_env_dict()
    assert env["PYTHONUTF8"] == "1"
