"""Argument validation of the `meiva.agents` entry points."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity, ExternalLease
from meiva import agents, gateway_client
from meiva.gateway_client import use_client
from meiva.sdk_surface import process_context


@pytest.fixture
def gateway_requests(sdk_environment: MeivaContext) -> Iterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        responses = {
            "/api/keyed/v1/agents": {"id": 42},
            "/api/agents/7/system-note": {"inbound_id": 81},
            "/api/agents/7/resurrect": {"status": "spawned"},
            "/api/agents/7/messages": None,
        }
        return httpx.Response(200, json=responses[request.url.path])

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(capture)) as client,
        use_client(client),
    ):
        yield requests


@pytest.fixture
def no_gateway_http(sdk_environment: MeivaContext) -> Iterator[None]:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid input reached HTTP: {request.method} {request.url.path}")

    with (
        httpx.Client(
            base_url="http://gateway", transport=httpx.MockTransport(unexpected)
        ) as client,
        use_client(client),
    ):
        yield


class TestAgentsEntries:
    def test_spawn_prompt_unwraps_before_gateway_call(
        self, gateway_requests: list[httpx.Request]
    ) -> None:
        result = agents.spawn(prompt=("hello",), machine="test-machine", idempotency_key="intent")  # pyright: ignore[reportArgumentType]
        assert result == 42
        [request] = gateway_requests
        assert (request.method, request.url.path) == ("POST", "/api/keyed/v1/agents")
        assert json.loads(request.content) == {
            "prompt": "hello",
            "prompt_source": "agent:1",
            "spawner": "agent:1",
            "machine": "test-machine",
        }

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param(("a", "b"), id="multi-element-tuple"),
            pytest.param(["a", "b"], id="multi-element-list"),
        ],
    )
    def test_spawn_prompt_multi_element_type_errors(
        self, no_gateway_http: None, prompt: object
    ) -> None:
        with pytest.raises(TypeError, match="prompt must be a string"):
            agents.spawn(prompt=prompt, idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_spawn_fork_from_never_unwraps(self, no_gateway_http: None) -> None:
        with pytest.raises(TypeError, match="fork_from must be int"):
            agents.spawn(fork_from=(5,), idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_send_message_content_unwraps(self, gateway_requests: list[httpx.Request]) -> None:
        agents.send_message(7, ("hi",))  # pyright: ignore[reportArgumentType]
        [request] = gateway_requests
        assert (request.method, request.url.path) == ("POST", "/api/agents/7/messages")
        assert json.loads(request.content) == {"content": "hi", "source": "agent:1"}

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

    def test_send_message_multi_element_type_errors(self, no_gateway_http: None) -> None:
        with pytest.raises(TypeError, match="content must be a string"):
            agents.send_message(7, ("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_send_system_note_unwraps_content_and_tag(
        self, gateway_requests: list[httpx.Request]
    ) -> None:
        result = agents.send_system_note(7, ("note",), tag=("task",), idempotency_key="note-intent")  # pyright: ignore[reportArgumentType]
        assert result == 81
        [request] = gateway_requests
        assert (request.method, request.url.path) == ("POST", "/api/agents/7/system-note")
        assert json.loads(request.content) == {
            "content": "note",
            "note_tag": "task",
            "source": "agent:1",
            "resurrect": True,
        }
        assert request.headers["Idempotency-Key"] == "note-intent"

    def test_resurrect_prompt_unwraps(self, gateway_requests: list[httpx.Request]) -> None:
        assert agents.resurrect(7, ("wake up",)) == "spawned"  # pyright: ignore[reportArgumentType]
        [request] = gateway_requests
        assert (request.method, request.url.path) == ("POST", "/api/agents/7/resurrect")
        assert json.loads(request.content) == {"prompt": "wake up", "resurrected_by": "agent:1"}

    def test_resurrect_rejects_none_prompt(self, no_gateway_http: None) -> None:
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
                lambda: agents.send_system_note(("7",), "hi", idempotency_key=str(uuid4())),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="send_system_note-id",
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_agent_ids_never_unwrap(self, no_gateway_http: None, call: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            call()


def test_core_spawn_forwards_caller_key(gateway_requests: list[httpx.Request]) -> None:
    assert agents.spawn(prompt="goal", machine="test-machine", idempotency_key="intent") == 42
    [request] = gateway_requests
    assert (request.method, request.url.path) == ("POST", "/api/keyed/v1/agents")
    assert json.loads(request.content) == {
        "prompt": "goal",
        "prompt_source": "agent:1",
        "spawner": "agent:1",
        "machine": "test-machine",
    }
    assert "require_idempotency" not in json.loads(request.content)
    assert request.headers["Idempotency-Key"] == "intent"
    assert request.headers["Idempotency-Scope"] == "principal-v1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"require_idempotency": 1, "idempotency_key": "key"},
        {"require_idempotency": True},
        {"idempotency_key": None},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
    ],
)
def test_core_spawn_invalid_strong_admission_before_actor_or_http(
    sdk_environment: MeivaContext, no_gateway_http: None, kwargs: dict[str, Any]
) -> None:
    def unexpected() -> int:
        raise AssertionError("invalid strong admission must precede actor lookup")

    lease = ExternalLease(agent_id=1, validate=unexpected, config=lambda: None)
    context = replace(
        sdk_environment, identity=AgentIdentity(agent_id=1, owns_loop=False, lease=lease)
    )
    previous = process_context.current_context()
    process_context.bind_context(context)
    try:
        with pytest.raises((TypeError, ValueError)):
            agents.spawn(**kwargs)
    finally:
        process_context.bind_context(previous)
