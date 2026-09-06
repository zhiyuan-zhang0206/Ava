"""Gateway test fixtures.

`POST /api/agents` and the lifecycle endpoints (terminate / resurrect /
restart) are HTTP-uniform: they always reach a runner via that runner's ops
server (`_forward_spawn_to_remote` / `_enqueue_lifecycle` ->
`dispatch_to_machine`; both live in routers/agents_forward.py), even when the
target is the co-located box (localhost). There is no in-process shortcut in
the router anymore.

In a unit test there is no live ops server listening, so a real
`dispatch_to_machine` would fail with 502. The autouse fixtures below stand in
for the local runner's ops daemon: they dispatch `launch_agent_op` /
`lifecycle_op` in-process against `app.state.db_pool` — exactly what the
runner's ops daemon does after receiving the forwarded op — so a plain
`client.post("/api/agents")` (or `/terminate` etc.) executes against the test
DB again.

Tests that exercise the forward routing itself (cross-machine spawn / the 400
no-agent-runner guard / lifecycle forward tests) install their own monkeypatch
over the same attribute, which runs after these autouse fixtures and wins.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr

from gateway.app import app
from gateway.routers import agents as _agents_router
from gateway.routers import agents_forward as _agents_forward_router
from ops.ops_lifecycle import launch_agent_op, lifecycle_op
from ops.rpc_schemas import LaunchAgentRequest, OpKind, SpawnedAgent
from shared import machines as _machines
from shared.config import settings as _settings
from shared.machine import machine_name


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all API keys to dummy values so spawn validation passes."""
    for attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "dashscope_api_key",
    ):
        monkeypatch.setattr(_settings.lm, attr, SecretStr("sk-test"))


@pytest.fixture(autouse=True)
def _local_spawn_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _in_process_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        # The gateway creates the agent row in-process (create_agent_row, real
        # DB); the runner's ops daemon dispatches launch_agent_op in-process —
        # mirror that here so a forwarded local launch produces a real child.
        return await launch_agent_op(body, app.state.db_pool)

    monkeypatch.setattr(_agents_router, "_forward_spawn_to_remote", _in_process_forward)

    # `POST /api/agents` reads the target's capability from the registry (the same
    # source the forward resolves the ops URL from) for every target, local
    # included — the gateway never introspects its own role. So a co-located
    # runner must resolve to agent-runner, as `ava start`'s register_self would
    # register the local machine. Names other than the local one fall through to
    # the real registry lookup, so the not-registered (404) and gateway-only (400)
    # guard tests still exercise it; a test wanting a different local capability
    # monkeypatches lookup_role itself (runs after this, wins).
    real_lookup_role = _machines.lookup_role

    def _lookup_role(name: str) -> list[str]:
        if name == machine_name():
            return ["gateway", "agent-runner"]
        return real_lookup_role(name)

    monkeypatch.setattr(_machines, "lookup_role", _lookup_role)

    # The spawn preflight also reads the pause latch (`is_paused`) for the
    # same target. The local machine is never paused in tests (the pause
    # endpoint refuses the gateway host itself), so stub it alongside the
    # role; remote names fall through to the real registry read.
    real_is_paused = _machines.is_paused

    def _is_paused(name: str) -> bool:
        if name == machine_name():
            return False
        return real_is_paused(name)

    monkeypatch.setattr(_machines, "is_paused", _is_paused)


@pytest.fixture(autouse=True)
def _local_lifecycle_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _in_process_lifecycle(target: str, path: str, json_body: dict) -> dict:
        # The runner's ops daemon dispatches lifecycle_op in-process; mirror
        # that here so a forwarded local terminate/resurrect/restart executes
        # against the test DB. AvaAgentError raises propagate directly — the
        # same exception types the wire round-trip would reconstruct. model_dump
        # mirrors the daemon serializing the response model onto the wire dict.
        return (await lifecycle_op(path, json_body, app.state.db_pool)).model_dump(mode="json")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_agents_forward_router, "_enqueue_lifecycle", _in_process_lifecycle)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture(autouse=True)
def _local_config_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same stand-in for the config router: a config_read / config_write op
    addressed to the LOCAL machine runs in-process (exactly what the co-located
    ops daemon would do after receiving the forwarded op), so the config panel
    tests exercise the router against the test .env. Remote targets still fall
    through to the real dispatch (tests stub it per-test). The router itself has
    NO in-process fallback — an unreachable ops server is a 503, uniform with
    every other machine."""
    from gateway.routers import config as _config_router
    from ops import ops_config

    real_dispatch = _config_router._cluster_rpc.dispatch_to_machine

    async def _in_process_config(
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        *,
        timeout_s: float | None = None,
        ops_url: str | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        # Mirror dispatch_to_machine's exact signature so positionally-called
        # tests (e.g. cluster_rpc's own unit tests) keep working. None forwards
        # to the real dispatch, which resolves AVA_CLUSTER_RPC_TIMEOUT_SECONDS
        # and the retry budget (task #961).
        if kind in ("config_read", "config_write"):
            assert target_machine == machine_name(), (
                "only the local machine is simulated in-process"
            )
            if kind == "config_read":
                return ops_config.config_read_op().model_dump(mode="json")
            return ops_config.config_write_op(
                cast("dict[str, Any]", payload["overrides"]),
                local=bool(payload.get("local", False)),
            ).model_dump(mode="json")
        # Not a config op — fall through to the real dispatch (other routers
        # share this module object and must keep their own behavior).
        return await real_dispatch(
            target_machine,
            cast("OpKind", kind),
            payload,
            timeout_s=timeout_s,
            ops_url=ops_url,
            retries=retries,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(_config_router._cluster_rpc, "dispatch_to_machine", _in_process_config)  # pyright: ignore[reportUnknownArgumentType]
