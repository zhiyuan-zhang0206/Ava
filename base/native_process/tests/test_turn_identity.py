"""The base-layer turn identity read: a bound turn wins over the AVA_AGENT_ID env identity, which is None when absent or malformed."""

from __future__ import annotations

import pytest

from base.native_process.turn_identity import (
    bind_turn_identity,
    current_turn_agent_id,
    effective_agent_id,
)


@pytest.fixture(autouse=True)
def _unset_agent_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env identity is the one ambient input these reads have besides the bound turn: start each test without it."""
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)


class TestEffectiveAgentId:
    def test_unbound_no_env_is_none(self) -> None:
        assert effective_agent_id() is None
        assert current_turn_agent_id() is None

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "42")
        assert effective_agent_id() == 42

    def test_bound_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "42")
        with bind_turn_identity(7):
            assert effective_agent_id() == 7
        assert effective_agent_id() == 42

    def test_malformed_env_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVA_AGENT_ID", "not-a-number")
        assert effective_agent_id() is None
