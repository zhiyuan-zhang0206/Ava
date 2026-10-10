"""Gateway HTTP calls use the explicit context's live transport policy inputs."""

from dataclasses import replace

import httpx
import pytest

import ava
from ava.gateway_client import memory_search
from ava.gateway_client.transport import GatewayTransportInputs, post
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet


def test_contexts_keep_live_deadlines_and_rebuild_inputs_without_an_sdk_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(ava, "context", raising=False)
    monkeypatch.delattr(ava, "__plugin_installation__", raising=False)
    first_values = {"attempts": 2.0, "delay": 0.0, "deadline": 8.0}
    second_values = {"attempts": 3.0, "delay": 0.0, "deadline": 21.0}
    reads: list[str] = []
    builds: list[str] = []
    seen: list[float] = []

    def context(label: str, values: dict[str, float]) -> AvaContext:
        def build() -> GatewayTransportInputs:
            builds.append(label)
            return GatewayTransportInputs(
                max_retries_reader=lambda: int(values["attempts"]),
                retry_delay_reader=lambda: values["delay"],
                memory_deadline_reader=read_deadline,
            )

        def read_deadline() -> float:
            reads.append(label)
            return values["deadline"]

        return AvaContext(clients=ClientSet(factories={GatewayTransportInputs: build}))

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"results": []})

    first, second = context("first", first_values), context("second", second_values)
    assert reads == builds == []
    with httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(answer)) as client:
        for owner in (first, second):
            with owner.clients.using_gateway(client):
                assert memory_search("query", 3, context=owner) == []
        first_values["deadline"] = 12.0
        for owner in (first, second):
            with owner.clients.using_gateway(client):
                assert memory_search("query", 3, context=owner) == []
        previous = first.clients.get(GatewayTransportInputs)
        first.clients.close()
        with first.clients.using_gateway(client):
            assert memory_search("query", 3, timeout=4.0, context=first) == []
        assert first.clients.get(GatewayTransportInputs) is not previous
    assert seen == [11.0, 24.0, 15.0, 24.0, 4.0]
    assert reads == ["first", "second", "first", "second"]
    assert builds == ["first", "second", "first"]
    second.clients.close()
    first.clients.close()


def test_partial_context_refuses_missing_transport_inputs() -> None:
    with pytest.raises(TypeError, match="max_retries_reader"):
        post("/api/x", context=AvaContext())


def test_input_construction_retains_readers_without_evaluating_them() -> None:
    def unexpected() -> float:
        raise AssertionError("transport construction must not read policy")

    inputs = GatewayTransportInputs(
        max_retries_reader=lambda: int(unexpected()),
        retry_delay_reader=unexpected,
        memory_deadline_reader=unexpected,
    )
    assert replace(inputs) == inputs


def test_attempt_count_is_read_once_and_each_backoff_reads_the_current_delay(
    retry_waits: list[float],
) -> None:
    values: dict[str, float] = {"attempts": 3, "delay": 1.0}
    trace: list[str] = []

    def attempts() -> int:
        trace.append("attempts")
        return int(values["attempts"])

    def delay() -> float:
        trace.append("delay")
        return values["delay"]

    inputs = GatewayTransportInputs(
        max_retries_reader=attempts,
        retry_delay_reader=delay,
        memory_deadline_reader=lambda: 15.0,
    )
    context = AvaContext(clients=ClientSet(factories={GatewayTransportInputs: lambda: inputs}))
    sends = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        trace.append("send")
        values["attempts"] = 1
        if sends < 3:
            values["delay"] = float(sends * 2)
            raise httpx.ConnectError("retry this attempt", request=request)
        return httpx.Response(200)

    with (
        httpx.Client(base_url="http://host.test", transport=httpx.MockTransport(answer)) as client,
        context.clients.using_gateway(client),
    ):
        assert post("/api/x", context=context).status_code == 200
    assert sends == 3
    assert retry_waits == [2.0, 8.0]
    assert trace == ["attempts", "send", "delay", "send", "delay", "send"]
    context.clients.close()
