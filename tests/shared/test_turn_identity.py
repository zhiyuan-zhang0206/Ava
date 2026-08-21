"""Turn-scoped agent identity (shared/turn_identity.py) and its layering into
`ava._boot` — Phase 1 of future/infra/agent-runner-as-server.md.

Locks the resolution order `turn contextvar > process slot > AVA_AGENT_ID env`
at every identity read, the copied-context handoff to worker threads, and the
process-mode invariants (nothing bound => behavior identical to today).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

import ava._boot as boot
from shared.turn_identity import (
    bind_turn_identity,
    current_turn_agent_id,
    effective_agent_id,
)


@pytest.fixture(autouse=True)
def _reset_process_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process bootstrap slots and the env identity per test."""
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setattr(boot, "_actor", None)
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


class TestBootLayering:
    def test_process_mode_unchanged(self) -> None:
        boot.establish(11, owns_loop=True)
        assert boot.agent_id() == 11
        assert boot.require_agent_id() == 11
        assert boot.require_actor() == "agent:11"
        boot.assert_self_action("restart")  # does not raise

    def test_turn_binding_wins_over_process_slot(self) -> None:
        boot.establish(11, owns_loop=True)
        with bind_turn_identity(22):
            assert boot.agent_id() == 22
            assert boot.require_agent_id() == 22
            assert boot.require_actor() == "agent:22"
            assert boot.default_actor() == "agent:22"
        assert boot.agent_id() == 11

    def test_turn_binding_provides_identity_without_process_slot(self) -> None:
        with bind_turn_identity(33):
            assert boot.require_agent_id() == 33
            boot.assert_self_action("terminate")  # turn context owns its loop

    def test_no_identity_still_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no established agent identity"):
            boot.require_agent_id()

    def test_launched_child_semantics_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A launched child (env identity, owns_loop False) still refuses
        # lifecycle self-actions — and a bound turn context is never a child.
        monkeypatch.setenv("AVA_AGENT_ID", "5")
        assert boot.is_launched_child() is True
        with pytest.raises(RuntimeError, match="background script"):
            boot.assert_self_action("restart")
        with bind_turn_identity(5):
            assert boot.is_launched_child() is False
            boot.assert_self_action("restart")  # the host owns the turn loop

    def test_explicit_actor_still_wins_without_turn_context(self) -> None:
        boot.establish_actor("schedule:7")
        assert boot.require_actor() == "schedule:7"
        with bind_turn_identity(9):
            # A turn context is more specific than the process actor: work done
            # inside agent 9's turn is agent 9's.
            assert boot.require_actor() == "agent:9"


class TestPropagation:
    def test_bind_reaches_asyncio_tasks(self) -> None:
        async def scenario() -> tuple[Any, Any]:
            async def turn() -> Any:
                return boot.agent_id()

            with bind_turn_identity(77):
                task = asyncio.create_task(turn())
            return await task, boot.agent_id()

        inside, outside = asyncio.run(scenario())
        assert inside == 77
        assert outside is None

    def test_copied_context_reaches_worker_thread(self) -> None:
        # The exec-node pattern: threads do not inherit contextvars, so the
        # worker must be started under contextvars.copy_context().
        import contextvars

        seen: list[Any] = []
        with bind_turn_identity(88):
            ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(lambda: seen.append(boot.agent_id()),))
        t.start()
        t.join()
        assert seen == [88]

    def test_bare_thread_does_not_inherit(self) -> None:
        seen: list[Any] = []
        with bind_turn_identity(88):
            t = threading.Thread(target=lambda: seen.append(current_turn_agent_id()))
            t.start()
            t.join()
        assert seen == [None]
