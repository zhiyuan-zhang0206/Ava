"""Argument validation of the `ava.agents` entry points."""

from __future__ import annotations

from typing import Any

import pytest

import ava
import ava.sdk_surface.agent_identity
from ava import agents, gateway_client


class TestAgentsEntries:
    def test_spawn_prompt_unwraps_before_gateway_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(gateway_client, "spawn", lambda **kw: seen.update(kw) or 1)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava.sdk_surface.agent_identity, "require_actor", lambda: "agent:1")

        agents.spawn(prompt=("hello",))  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == "hello"

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param(("a", "b"), id="multi-element-tuple"),
            pytest.param(["a", "b"], id="multi-element-list"),
        ],
    )
    def test_spawn_prompt_multi_element_type_errors(
        self, monkeypatch: pytest.MonkeyPatch, prompt: object
    ) -> None:
        monkeypatch.setattr(gateway_client, "spawn", lambda **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string"):
            agents.spawn(prompt=prompt)  # pyright: ignore[reportArgumentType]

    def test_spawn_fork_from_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway_client, "spawn", lambda **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="fork_from must be int"):
            agents.spawn(fork_from=(5,))  # pyright: ignore[reportArgumentType]

    def test_send_message_content_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "send_message",
            lambda _aid, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.send_message(7, ("hi",))  # pyright: ignore[reportArgumentType]
        assert seen["content"] == "hi"

    def test_send_message_blocks_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "send_message",
            lambda _aid, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        blocks: list[dict[str, object]] = [{"type": "text", "text": "hi"}]

        agents.send_message(7, blocks)  # pyright: ignore[reportArgumentType]
        assert seen["content"] is blocks

    def test_send_message_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway_client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="content must be a string"):
            agents.send_message(7, ("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_send_system_note_unwraps_content_and_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "send_system_note",
            lambda *_a, **_kw: seen.update(_kw) or 1,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.send_system_note(7, ("note",), tag=("task",))  # pyright: ignore[reportArgumentType]
        assert seen["content"] == "note"
        assert seen["note_tag"] == "task"

    def test_resurrect_prompt_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "resurrect",
            lambda *_a, **_kw: seen.update(_kw) or "spawned",  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.resurrect(7, ("wake up",))  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == "wake up"

    def test_resurrect_rejects_none_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway_client, "resurrect", lambda *_a, **_kw: "spawned")  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string, got None"):
            agents.resurrect(7, None)  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(lambda: agents.terminate(("7",)), "agent_id must be int", id="terminate"),  # pyright: ignore[reportArgumentType]
            pytest.param(lambda: agents.restart(("7",)), "agent_id must be int", id="restart"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_status(("7",)),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="get_status",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_neighbors(("7",)),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="get_neighbors",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_neighbors(7, depth=("1",)),  # pyright: ignore[reportArgumentType]
                "depth must be int",
                id="depth",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.send_message(("7",), "hi"),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="send_message-id",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.send_system_note(("7",), "hi"),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="send_system_note-id",
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_agent_ids_never_unwrap(
        self, monkeypatch: pytest.MonkeyPatch, call: Any, match: str
    ) -> None:
        monkeypatch.setattr(gateway_client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "send_system_note", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "terminate", lambda *_a, **_kw: "enqueued")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "restart", lambda *_a, **_kw: "enqueued")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "list_agents", lambda **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "get_neighbors", lambda *_a, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match=match):
            call()
