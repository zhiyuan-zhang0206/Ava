"""`ava.gateway_client.patch_label` — the wire shape of an agent's label change."""

import json

import httpx

from ava import gateway_client
from ava.gateway_client import use_client
from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs


def test_patch_label_sends_the_label_and_who_set_it() -> None:
    sent: list[tuple[str, str, object]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    context = AvaContext(
        clients=ClientSet(
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: 1,
                    retry_delay_reader=lambda: 0.0,
                    memory_deadline_reader=lambda: 1.0,
                )
            }
        )
    )
    previous = process_context.bound_context()
    process_context.bind_context(context)
    try:
        with (
            httpx.Client(
                base_url="http://gateway", transport=httpx.MockTransport(capture)
            ) as client,
            use_client(client),
        ):
            gateway_client.patch_label(42, "lead")
            gateway_client.patch_label(42, "", source="user")

        assert sent == [
            ("PATCH", "/api/agents/42", {"label": "lead", "source": "self"}),
            ("PATCH", "/api/agents/42", {"label": "", "source": "user"}),
        ]
    finally:
        try:
            context.clients.close()
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)
