import httpx
import pytest
from fastapi import HTTPException

from base.agents.to_user import ToUserReadRequest
from base.config import settings
from gateway.agents.to_user.document import fetch_resource, resource_request, resource_response


def test_html_renders_inline_in_a_script_only_sandbox() -> None:
    html = resource_response(b"<script>alert(1)</script>", 'test".html')
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers["content-disposition"].startswith("inline;")
    assert "%22" in html.headers["content-disposition"]
    policy = html.headers["content-security-policy"]
    assert policy.startswith("sandbox allow-scripts;")
    assert "allow-same-origin" not in policy
    assert "default-src 'none'" in policy
    assert "img-src data: https:" in policy
    assert html.headers["cache-control"] == "no-store"
    assert html.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name",
    [
        "plot.png",
        "plot.svg",
        "voice.mp3",
        "clip.mp4",
        "report.pdf",
        "notes.md",
        "log.txt",
        "data.json",
        "table.csv",
        "feed.xml",
    ],
)
def test_passive_files_render_inline_without_scripts(name: str) -> None:
    response = resource_response(b"payload", name)
    assert response.headers["content-disposition"].startswith("inline;")
    policy = response.headers["content-security-policy"]
    assert policy.startswith("sandbox;")
    assert "allow-scripts" not in policy
    assert "default-src 'none'" in policy


@pytest.mark.parametrize(
    "name",
    ["tool.sh", "bundle.js", "archive.zip", "setup.exe", "report.docx", "book.xlsx", "blob.bin"],
)
def test_active_archives_and_unknown_files_stay_attachments(name: str) -> None:
    response = resource_response(b"payload", name)
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize(
    "references", [["/etc/passwd"], ["a" * 64] * (settings.to_user.max_chain_depth + 1)]
)
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
async def _oversize_stream():
    chunk = b"x" * (4 * 1024 * 1024)
    remaining = settings.to_user.max_resource_bytes + 1
    while remaining > 0:
        yield chunk[: min(len(chunk), remaining)]
        remaining -= len(chunk)


async def test_missing_file_and_oversize_fail_explicitly() -> None:
    for status, content, expected in [
        (404, b"gone", 404),
        (200, _oversize_stream(), 413),
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
