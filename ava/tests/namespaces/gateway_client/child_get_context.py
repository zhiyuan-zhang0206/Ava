"""Run SDK context refusal and explicit host transport in a fresh interpreter."""

import httpx

from ava.gateway_client import get, get_launch_attempt, retry_launch
from ava.sdk_surface import process_context
from base.agents.context import AvaContext


def main() -> None:
    try:
        get_launch_attempt(0)
    except ValueError as exc:
        assert "positive agent ID" in str(exc)
    else:
        raise AssertionError("invalid agent ID must be rejected before ambient context")
    try:
        retry_launch(42, idempotency_key="", expected_prior_attempt_id="invalid")
    except ValueError as exc:
        assert "idempotency key" in str(exc)
    else:
        raise AssertionError("invalid admission must be rejected before ambient context")
    for shared_host in (False, True):
        if shared_host:
            process_context.bind_host_process()
        try:
            get("/api/read")
        except process_context.ContextOutsideProcessError as exc:
            assert "context" in str(exc)
        else:
            raise AssertionError("ambient GET must refuse an unbound or shared host")
    context = AvaContext()

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://explicit/api/read?q=host"
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, json={"ok": True})

    with (
        httpx.Client(
            base_url="http://explicit", timeout=7, transport=httpx.MockTransport(answer)
        ) as client,
        context.clients.using_gateway(client),
    ):
        assert get("/api/read", params={"q": "host"}, max_retries=1, context=context).json() == {
            "ok": True
        }
    context.clients.close()


if __name__ == "__main__":
    main()
