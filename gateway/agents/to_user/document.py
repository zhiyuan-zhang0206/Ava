import asyncio
import mimetypes
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError

from base.agents.to_user import MAX_RESOURCE_BYTES, ToUserDocument, ToUserReadRequest
from base.cluster.machine import gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machines import MachineGatewayUrlMissing, MachineNotRegistered, lookup
from gateway.agents.eval_guard import deny_isolated_result_read
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable, dispatch_to_machine

router = APIRouter()


def home_machine(request: Request, agent_id: int) -> str:
    deny_isolated_result_read(request)
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Agent not found")
    return row[0]


def resource_request(agent_id: int, references: list[str]) -> ToUserReadRequest:
    try:
        return ToUserReadRequest(agent_id=agent_id, references=references)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid document reference") from exc


async def read_snapshot(request: Request, query: ToUserReadRequest, machine: str) -> ToUserDocument:
    try:
        result = await dispatch_to_machine(
            request.app.state.db,
            target_machine=machine,
            kind="to_user_read",
            payload=query.model_dump(),
            timeout_s=3,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except ClusterOpFailed as exc:
        raise HTTPException(422, "The workspace document could not be read") from exc
    return ToUserDocument.model_validate(result)


@router.get("/api/agents/{agent_id}/to-user")
async def get_to_user(
    agent_id: int,
    request: Request,
    reference: Annotated[list[str] | None, Query()] = None,
) -> ToUserDocument:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    return await read_snapshot(request, resource_request(agent_id, reference or []), machine)


def attachment_response(content: bytes, name: str) -> Response:

    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = (
        media_type.startswith(("image/", "audio/", "video/")) or media_type == "application/pdf"
    )
    disposition = "inline" if inline else "attachment"
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
            "Referrer-Policy": "no-referrer",
        },
    )


async def fetch_resource(
    client: httpx.AsyncClient, url: str, query: ToUserReadRequest, headers: dict[str, str]
) -> bytes:

    async with client.stream("POST", url, json=query.model_dump(), headers=headers) as response:
        if response.status_code != 200:
            status = response.status_code if response.status_code in (404, 422, 503) else 502
            raise HTTPException(status, "Referenced file could not be read")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MAX_RESOURCE_BYTES:
                raise HTTPException(413, "Referenced file exceeds the resource limit")
        return bytes(content)


@router.get("/api/agents/{agent_id}/to-user/resource")
async def get_resource(
    agent_id: int,
    request: Request,
    reference: Annotated[list[str], Query(min_length=1, max_length=8)],
) -> Response:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    query = resource_request(agent_id, reference)
    parent = resource_request(agent_id, query.references[:-1])
    document = await read_snapshot(request, parent, machine)
    resource = next(
        (value for value in document.resources.values() if value.id == reference[-1]), None
    )
    if resource is None:
        raise HTTPException(404, "The document no longer references this resource")
    access = gateway_access_from_boot(request.app.state.gateway_process.config)
    try:
        ops_url = await asyncio.to_thread(lookup, request.app.state.db, machine)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            content = await fetch_resource(
                client,
                f"{ops_url.rstrip('/')}/to-user/resource",
                query,
                gateway_auth_headers(access),
            )
    except (httpx.HTTPError, MachineGatewayUrlMissing, MachineNotRegistered) as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    return attachment_response(content, resource.name)
