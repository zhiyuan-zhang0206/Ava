import httpx
import pytest
from fastapi import HTTPException

from base.agents.to_user import MAX_RESOURCE_BYTES, ToUserReadRequest
from gateway.agents.to_user.document import attachment_response, fetch_resource, resource_request


def test_active_files_download_and_previews_are_sandboxed() -> None:
    html = attachment_response(b"<script>alert(1)</script>", 'test".html')
    assert html.headers["content-disposition"].startswith("attachment;")
    assert "%22" in html.headers["content-disposition"]
    svg = attachment_response(b"<svg/>", "plot.svg")
    assert svg.headers["content-disposition"].startswith("inline;")
    assert "sandbox;" in svg.headers["content-security-policy"]
    assert "allow-scripts" not in svg.headers["content-security-policy"]
    assert svg.headers["cache-control"] == "no-store"
    assert svg.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("references", [["/etc/passwd"], ["a" * 64] * 9])
def test_client_paths_and_unbounded_chains_are_rejected(references: list[str]) -> None:
    with pytest.raises(HTTPException) as error:
        resource_request(1, references)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_binary_response_and_auth_are_preserved() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test"
        assert request.url.path == "/to-user/resource"
        return httpx.Response(200, content=b"\x00\xffbinary")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await fetch_resource(
            client,
            "http://runner/to-user/resource",
            ToUserReadRequest(agent_id=1),
            {"Authorization": "Bearer test"},
        )
    assert result == b"\x00\xffbinary"


@pytest.mark.asyncio
async def test_missing_file_and_oversize_fail_explicitly() -> None:
    for status, content, expected in [
        (404, b"gone", 404),
        (200, b"x" * (MAX_RESOURCE_BYTES + 1), 413),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, status=status, content=content: httpx.Response(status, content=content)
            )
        ) as client:
            with pytest.raises(HTTPException) as error:
                await fetch_resource(
                    client, "http://runner/to-user/resource", ToUserReadRequest(agent_id=1), {}
                )
            assert error.value.status_code == expected
