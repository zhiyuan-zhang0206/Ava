"""Gateway REST and SSE client for the IM Bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from services.im_bridge.types import AgentRow
from shared.config import settings

_log = logging.getLogger("services.im_bridge.gateway_client")


class GatewayClient:
    """REST + SSE client for the Ava gateway (the Post Gateway)."""

    def __init__(self) -> None:
        self._base = settings.gateway.gateway_url.rstrip("/")
        self._cookie: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base, timeout=httpx.Timeout(30.0, connect=10.0)
            )
        return self._client

    async def login(self) -> None:
        """POST /api/auth/login with the cluster secret; keep the session cookie."""
        client = await self._http()
        resp = await client.post(
            "/api/auth/login",
            json={"password": settings.data_plane.cluster_secret},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"gateway login failed: HTTP {resp.status_code}")
        self._cookie = resp.headers.get("set-cookie", "")

    def _headers(self) -> dict[str, str]:
        return {"Cookie": self._cookie} if self._cookie else {}

    async def list_agents(self) -> list[AgentRow]:
        """GET /api/agents → list of agent rows (id/label/status/...)."""
        client = await self._http()
        resp = await client.get("/api/agents", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"list agents failed: HTTP {resp.status_code}")
        return resp.json()

    async def send_message(
        self, agent_id: int, text: str, *, idempotency_key: str | None = None
    ) -> None:
        """POST /api/agents/{id}/messages — enqueue a chat inbound; retries with
        backoff (an IM message must not drop once the platform offset moved).
        AtLeastOnceWithKey: same Idempotency-Key on every retry, so the server
        dedups — never a duplicate even after commit. Outbox replays pass
        their persisted key (Task #1032), so a replay after a lost gateway
        response stays a no-op server-side."""
        key = idempotency_key or uuid.uuid4().hex
        for attempt, delay in enumerate(settings.services.im_send_retry_delays, start=1):
            try:
                client = await self._http()
                resp = await client.post(
                    f"/api/agents/{agent_id}/messages",
                    headers={**self._headers(), "Idempotency-Key": key},
                    # IM is a frontend like the web composer — the human speaking
                    # through any channel is just "user".
                    json={"content": text, "source": "user"},
                )
                if resp.status_code == 201:
                    return
                if resp.status_code >= 500:
                    # gateway mid-rollout — retry; 4xx (validation) never retries
                    _log.warning(
                        "send to agent %s failed: HTTP %s (attempt %d, retry in %.0fs)",
                        agent_id,
                        resp.status_code,
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"send to agent {agent_id} failed: HTTP {resp.status_code}")
            except httpx.HTTPError:
                _log.warning(
                    "send to agent %s failed (attempt %d, retry in %.0fs)",
                    agent_id,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(
            f"send to agent {agent_id} failed after {len(settings.services.im_send_retry_delays)} attempts"
        )

    async def list_presets(self) -> list[dict[str, Any]]:
        """GET /api/presets — spawn menu layer 1: [{id, name, label,
        description, config}], ordered by name."""
        client = await self._http()
        resp = await client.get("/api/presets", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"list presets failed: HTTP {resp.status_code}")
        return resp.json()

    async def list_models(self) -> dict[str, Any]:
        """GET /api/models — spawn menu layer 2: {providers, models, default}."""
        client = await self._http()
        resp = await client.get("/api/models", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"list models failed: HTTP {resp.status_code}")
        return resp.json()

    async def spawn_agent(self, *, preset: str | None, config: dict[str, object] | None) -> int:
        """POST /api/agents — create an agent, return its id. The gateway
        folds the named preset into config; the runner never sees it."""
        client = await self._http()
        payload: dict[str, object] = {"spawner": "user"}
        if preset is not None:
            payload["preset"] = preset
        if config:
            payload["config"] = config
        resp = await client.post("/api/agents", headers=self._headers(), json=payload)
        if resp.status_code != 201:
            raise RuntimeError(f"spawn failed: HTTP {resp.status_code} - {resp.text[:300]}")
        return int(resp.json()["id"])

    async def list_commands(self) -> list[dict[str, Any]]:
        """GET /api/commands — the Ava slash-command catalog (every active
        skill gets a same-named command; project/user/plugin templates add
        more). name + description + instruction_hint, deduped and sorted."""
        client = await self._http()
        resp = await client.get("/api/commands", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"list commands failed: HTTP {resp.status_code}")
        return resp.json()

    async def get_timeline(self, agent_id: int, limit: int = 5) -> list[dict[str, Any]]:
        """GET /api/agents/{id}/timeline?limit=N → rendered TimelineItems."""
        client = await self._http()
        resp = await client.get(
            f"/api/agents/{agent_id}/timeline",
            headers=self._headers(),
            params={"limit": limit},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"timeline {agent_id} failed: HTTP {resp.status_code}")
        return resp.json().get("items", [])

    async def stream_events(
        self, agent_id: int
    ) -> AsyncGenerator[dict[str, Any], None]:  # pragma: no cover - generator
        """SSE GET /api/agents/{id}/events/stream; yield parsed event dicts."""
        client = await self._http()
        async with client.stream(
            "GET",
            f"/api/agents/{agent_id}/events/stream",
            headers=self._headers(),
            timeout=httpx.Timeout(settings.services.im_sse_read_timeout_seconds, connect=10.0),
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"sse {agent_id} failed: HTTP {resp.status_code}")
            buf = ""
            async for chunk in resp.aiter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    data = None
                    for line in frame.splitlines():
                        if line.startswith("data:"):
                            data = line[5:].strip()
                    if data and data != '{"role":"heartbeat"}':
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            _log.warning("sse unparseable frame: %.120s", data)
