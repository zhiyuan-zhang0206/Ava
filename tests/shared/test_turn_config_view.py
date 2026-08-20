"""Per-turn settings view (shared/config/turn_view.py) — the agent-scoping
substrate for the hosted runner (future/infra/agent-runner-as-server.md, work
item b).

Locks the four contracts the hosted dispatcher will build on:

1. **Process-mode equivalence** — with nothing bound, `turn_settings.x.y` IS
   `settings.x.y`, including seeing live singleton mutations.
2. **Pin resolution** — a bound overlay pin wins over the singleton for its
   own field only; an unpinned per-agent field stays LIVE (a singleton edit
   mid-turn is visible), per the `lifecycle: live` contract.
3. **Merge precedence** — `config_overlay > birth_config`, unknown/plugin keys
   dropped, exactly mirroring the process-boot merge.
4. **Context propagation** — a bind before `asyncio.create_task` reaches the
   task AND tasks the task itself spawns (the LangGraph node-task shape);
   sibling tasks created outside the bind never see the pins.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from shared.config import (
    bind_agent_config,
    resolve_agent_config_pins,
    settings,
    turn_settings,
)
from shared.config.turn_view import current_agent_config_pins


class TestProcessModeEquivalence:
    def test_unbound_reads_the_singleton(self) -> None:
        assert turn_settings.lm.llm_model == settings.lm.llm_model
        assert turn_settings.agent.sdk_disable == settings.agent.sdk_disable

    def test_unbound_sees_live_singleton_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_model", "test-model-live")
        assert turn_settings.lm.llm_model == "test-model-live"

    def test_non_per_agent_field_reads_singleton(self) -> None:
        # db_url is not per_agent; the proxy must fall through unconditionally.
        assert turn_settings.data_plane.db_url == settings.data_plane.db_url

    def test_non_domain_attributes_delegate(self) -> None:
        assert turn_settings.has_domain("lm") is settings.has_domain("lm")

    def test_view_is_read_only(self) -> None:
        with pytest.raises(AttributeError, match="read-only"):
            turn_settings.lm = object()  # type: ignore[assignment]


class TestPinResolution:
    def test_pin_wins_over_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_model", "cluster-default-model")
        with bind_agent_config({"llm_model": "pinned-model"}):
            assert turn_settings.lm.llm_model == "pinned-model"
        assert turn_settings.lm.llm_model == "cluster-default-model"

    def test_unpinned_field_stays_live_inside_bind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # lifecycle:live contract — an unpinned per-agent field keeps reading
        # the singleton at access time, so a cluster edit mid-turn is visible.
        with bind_agent_config({"llm_model": "pinned-model"}):
            monkeypatch.setattr(settings.lm, "reasoning_effort", "test-effort")
            assert turn_settings.lm.reasoning_effort == "test-effort"

    def test_pin_only_applies_to_owning_domain(self) -> None:
        # A pin named like an lm field must not shadow an attribute lookup on
        # a different domain proxy (flat names are unique, but the proxy must
        # verify ownership rather than trust the name).
        with bind_agent_config({"llm_model": "pinned-model"}):
            assert turn_settings.agent.sdk_disable == settings.agent.sdk_disable

    def test_nested_bind_restores_outer(self) -> None:
        with bind_agent_config({"llm_model": "outer"}):
            with bind_agent_config({"llm_model": "inner"}):
                assert turn_settings.lm.llm_model == "inner"
            assert turn_settings.lm.llm_model == "outer"
        assert current_agent_config_pins() is None


class TestResolvePins:
    def test_overlay_beats_birth(self) -> None:
        pins = resolve_agent_config_pins({"llm_model": "from-overlay"}, {"llm_model": "from-birth"})
        assert pins == {"llm_model": "from-overlay"}

    def test_birth_fills_fields_overlay_omits(self) -> None:
        pins = resolve_agent_config_pins(
            {"llm_model": "from-overlay"}, {"reasoning_effort": "from-birth"}
        )
        assert pins == {
            "llm_model": "from-overlay",
            "reasoning_effort": "from-birth",
        }

    def test_unknown_and_plugin_keys_dropped(self) -> None:
        pins = resolve_agent_config_pins(
            {"llm_model": "m", "ava_memory.pool_size": 3, "deleted_field": 1},
            None,
        )
        assert pins == {"llm_model": "m"}

    def test_none_maps(self) -> None:
        assert resolve_agent_config_pins(None, None) == {}


class TestContextPropagation:
    def test_bind_before_create_task_reaches_task_and_child_tasks(self) -> None:
        async def scenario() -> tuple[Any, Any, Any]:
            async def node_task() -> Any:
                # The LangGraph shape: the turn task spawns node tasks; each
                # copies the loop-level context, so the pin must still be there.
                return turn_settings.lm.llm_model

            async def turn_task() -> tuple[Any, Any]:
                direct = turn_settings.lm.llm_model
                child = await asyncio.create_task(node_task())
                return direct, child

            with bind_agent_config({"llm_model": "agent-7-model"}):
                task = asyncio.create_task(turn_task())
            # Bound context was reset before awaiting — the task must have
            # captured it at creation.
            sibling = turn_settings.lm.llm_model
            direct, child = await task
            return direct, child, sibling

        direct, child, sibling = asyncio.run(scenario())
        assert direct == "agent-7-model"
        assert child == "agent-7-model"
        assert sibling == settings.lm.llm_model

    def test_concurrent_tasks_see_their_own_pins(self) -> None:
        async def scenario() -> list[Any]:
            started = asyncio.Event()

            async def read_model(release: asyncio.Event, wait: asyncio.Event) -> Any:
                release.set()
                await wait.wait()
                return turn_settings.lm.llm_model

            done_a = asyncio.Event()
            with bind_agent_config({"llm_model": "agent-a"}):
                task_a = asyncio.create_task(read_model(started, done_a))
            await started.wait()
            with bind_agent_config({"llm_model": "agent-b"}):
                task_b = asyncio.create_task(read_model(asyncio.Event(), done_a))
            done_a.set()
            return list(await asyncio.gather(task_a, task_b))

        assert asyncio.run(scenario()) == ["agent-a", "agent-b"]
