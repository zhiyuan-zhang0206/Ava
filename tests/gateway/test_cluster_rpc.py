"""Unit tests for gateway/cluster_rpc.py:dispatch_to_machine.

This is the single gateway -> agent-runner call site for every cluster op,
and its job is almost entirely error classification. Every consumer test mocks
`dispatch_to_machine` and asserts on the exception types it is *supposed* to
raise, so without these tests nothing proves the real function honors that
contract. Each test drives one outcome arm over a fake HTTP transport.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from ops import cluster_rpc
from shared.machines import MachineGatewayUrlMissing, MachineNotRegistered


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str = "http://host:8106",
    handler=None,  # type: ignore[no-untyped-def]
    lookup_exc: Exception | None = None,
) -> dict[str, Any]:
    """Patch the machines lookup + httpx.AsyncClient. Returns a dict that captures
    the outgoing request(s) so the test can assert on URL / body: `request` is the
    last one, `requests` is every one (retry tests need all attempts)."""
    captured: dict[str, Any] = {}

    def _lookup(name: str) -> str:
        if lookup_exc is not None:
            raise lookup_exc
        return url

    monkeypatch.setattr(cluster_rpc, "lookup_machine_url", _lookup)

    if handler is not None:
        real_client = httpx.AsyncClient  # capture before patching to avoid recursion

        def _capturing_handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            captured.setdefault("requests", []).append(request)
            return handler(request)

        def _client_factory(**kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(_capturing_handler), **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cluster_rpc.httpx, "AsyncClient", _client_factory)
    return captured


@pytest.mark.asyncio
async def test_completed_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 + status=completed -> returns the result dict; the request hits /ops with the body."""
    captured = _patch(
        monkeypatch,
        url="http://host:8106",
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {"id": 5}}),
    )
    result = await cluster_rpc.dispatch_to_machine(
        "wsl", "spawn-launch", {"prompt": "hi"}, retries=0
    )
    assert result == {"id": 5}
    req = captured["request"]
    assert str(req.url) == "http://host:8106/ops"
    import json

    body = json.loads(req.content)
    assert body["kind"] == "spawn-launch"
    assert body["payload"] == {"prompt": "hi"}
    # spawn is non-idempotent: the envelope carries the auto-generated dedup key.
    assert body["idempotency_key"].startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_failed_raises_cluster_op_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 + status=failed -> ClusterOpFailed carrying the result payload."""
    _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(
            200, json={"status": "failed", "result": {"error": "boom"}}
        ),
    )
    with pytest.raises(cluster_rpc.ClusterOpFailed) as ei:
        await cluster_rpc.dispatch_to_machine("wsl", "lifecycle", {}, retries=0)
    assert ei.value.result == {"error": "boom"}


@pytest.mark.asyncio
async def test_unknown_status_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status outside {completed, failed} fails OpResponse validation -> a
    malformed-response ClusterOpUnreachable, never a silently-returned garbage result."""
    _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "pending", "result": {}}),
    )
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)


@pytest.mark.asyncio
async def test_non_200_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 from the ops server -> ClusterOpUnreachable (not ClusterOpFailed)."""
    _patch(monkeypatch, handler=lambda _r: httpx.Response(503, text="ops down"))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="503"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)


@pytest.mark.asyncio
async def test_httpx_error_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error (connect/read timeout) -> ClusterOpUnreachable."""

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch(monkeypatch, handler=_boom)
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="unreachable"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)


@pytest.mark.asyncio
async def test_status_probe_unreachable_logs_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P0: a status_probe reachability failure logs at DEBUG (an offline host at
    panel cadence would otherwise flood the log with WARNINGs)."""

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch(monkeypatch, handler=_boom)
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster_rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)
    recs = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs)


@pytest.mark.asyncio
async def test_non_probe_unreachable_stays_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P0 boundary: only status_probe is downgraded — an operator-initiated op
    (spawn / lifecycle / ...) keeps its unreachable failure at WARNING."""

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch(monkeypatch, handler=_boom)
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster_rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await cluster_rpc.dispatch_to_machine("wsl", "spawn-launch", {}, retries=0)
    recs = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert recs and all(r.levelno == logging.WARNING for r in recs)


@pytest.mark.asyncio
async def test_unregistered_machine_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing machines row -> ClusterOpUnreachable (translated from MachineNotRegistered)."""
    _patch(monkeypatch, lookup_exc=MachineNotRegistered("no machine named 'ghost'"))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="cannot resolve an address"):
        await cluster_rpc.dispatch_to_machine("ghost", "status_probe", {})


@pytest.mark.asyncio
async def test_null_gateway_url_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with NULL gateway_url (host not yet started on new code) ->
    ClusterOpUnreachable (translated from MachineGatewayUrlMissing)."""
    _patch(monkeypatch, lookup_exc=MachineGatewayUrlMissing("wsl advertises no gateway_url"))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="cannot resolve an address"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)


@pytest.mark.asyncio
async def test_provided_ops_url_bypasses_machines_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-resolved `ops_url` dials directly and NEVER reads the machines table:
    the lookup here is wired to raise, yet the dispatch still round-trips to the
    given URL. This is what keeps the failed-rollout compensating resume
    Postgres-free — the 2026-07-20 incident, where the resume's own machines read
    raised (data plane down) and left every host stop-the-world + paused."""
    captured = _patch(
        monkeypatch,
        lookup_exc=MachineNotRegistered("lookup must not run when ops_url is provided"),
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {"ok": 1}}),
    )
    result = await cluster_rpc.dispatch_to_machine(
        "wsl", "cluster_resume", {}, ops_url="http://direct:8106"
    )
    assert result == {"ok": 1}
    assert str(captured["request"].url) == "http://direct:8106/ops"


@pytest.mark.asyncio
async def test_default_timeout_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting timeout_s uses AVA_CLUSTER_RPC_TIMEOUT_SECONDS (task #698 G8)
    — the default is config, not a module literal."""
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "cluster_rpc_timeout_seconds", 12.5)
    monkeypatch.setattr(cluster_rpc, "lookup_machine_url", lambda _n: "http://host:8106")  # pyright: ignore[reportUnknownArgumentType]
    real_client = httpx.AsyncClient
    seen: dict[str, httpx.Timeout] = {}

    def _client_factory(**kwargs):  # type: ignore[no-untyped-def]
        seen["timeout"] = kwargs["timeout"]
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
        )
        return real_client(**kwargs)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(cluster_rpc.httpx, "AsyncClient", _client_factory)  # pyright: ignore[reportUnknownArgumentType]
    await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)
    assert seen["timeout"].read == 12.5


@pytest.mark.asyncio
async def test_client_ignores_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluster-private RPC dials never inherit the host's proxy settings."""
    monkeypatch.setattr(cluster_rpc, "lookup_machine_url", lambda _n: "http://host:8106")  # pyright: ignore[reportUnknownArgumentType]
    real_client = httpx.AsyncClient
    seen: dict[str, object] = {}

    def _client_factory(**kwargs):  # type: ignore[no-untyped-def]
        seen["trust_env"] = kwargs["trust_env"]
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
        )
        return real_client(**kwargs)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(cluster_rpc.httpx, "AsyncClient", _client_factory)  # pyright: ignore[reportUnknownArgumentType]
    await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=0)
    assert seen["trust_env"] is False


@pytest.mark.asyncio
async def test_connect_timeout_capped_by_timeout_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short probe timeout_s bounds the connect phase too. The connect floor
    used to be a flat 10s, so a blackholed host (powered-off private-network peer)
    stretched every 3s roster probe to ~10s and /api/status with it."""
    monkeypatch.setattr(cluster_rpc, "lookup_machine_url", lambda _n: "http://host:8106")  # pyright: ignore[reportUnknownArgumentType]
    real_client = httpx.AsyncClient
    seen: dict[str, httpx.Timeout] = {}

    def _client_factory(**kwargs):  # type: ignore[no-untyped-def]
        seen["timeout"] = kwargs["timeout"]
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
        )
        return real_client(**kwargs)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(cluster_rpc.httpx, "AsyncClient", _client_factory)  # pyright: ignore[reportUnknownArgumentType]
    await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, timeout_s=3.0)
    assert seen["timeout"].connect == 3.0


# ─── retry loop ──────────────────────────────────────────────────────────────


def _pin_retry(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the retry machinery deterministic: fixed (un-jittered) delays and a
    recorded sleep log. Returns the list of delays actually slept."""
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(cluster_rpc, "_sleep", _fake_sleep)
    monkeypatch.setattr(cluster_rpc.random, "uniform", lambda _a, _b: 1.0)  # pyright: ignore[reportUnknownArgumentType]
    return sleeps


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection reset on the first attempt is retried once; the op succeeds
    on the fresh second connection."""
    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("connection reset by peer")
        return httpx.Response(200, json={"status": "completed", "result": {"ok": 1}})

    captured = _patch(monkeypatch, handler=_flaky)
    sleeps = _pin_retry(monkeypatch)

    result = await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=1)

    assert result == {"ok": 1}
    assert calls["n"] == 2
    assert len(captured["requests"]) == 2
    assert sleeps == [0.5]  # base delay, attempt 0 → 0.5s (jitter pinned to 1.0)


@pytest.mark.asyncio
async def test_two_connection_resets_exhaust_single_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fast transport failures consume the initial attempt plus one retry,
    then surface ClusterOpUnreachable to the roster's offline/backoff path."""

    def _reset(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset by peer")

    captured = _patch(monkeypatch, handler=_reset)
    sleeps = _pin_retry(monkeypatch)

    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 2 attempt"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=1)

    assert len(captured["requests"]) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_fetch_retries_are_warning_other_kinds_stay_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`cluster_fetch` intermediate retries log at WARNING while every other
    kind stays DEBUG — a fetch retry is not cheap retry machinery, it is a full
    30s host-side `git fetch` that ran and died (two timeouts then success on
    win/wsl, 2026-08-27), and the rollout log only carries WARNING+ from the
    detached session. Every attempt must be visible where Phase 0 is read."""

    def _flaky(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    _patch(monkeypatch, handler=_flaky)
    _pin_retry(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="ops.cluster_rpc"):
        with pytest.raises(cluster_rpc.ClusterOpUnreachable):
            await cluster_rpc.dispatch_to_machine("win", "cluster_fetch", {}, retries=1)
        with pytest.raises(cluster_rpc.ClusterOpUnreachable):
            await cluster_rpc.dispatch_to_machine("win", "status_probe", {}, retries=1)

    retry_lines = [r for r in caplog.records if "retrying in" in r.getMessage()]
    assert len(retry_lines) == 2  # one intermediate retry line per dispatch
    fetch_line = [r for r in retry_lines if "cluster_fetch" in r.getMessage()]
    probe_line = [r for r in retry_lines if "status_probe" in r.getMessage()]
    assert fetch_line and fetch_line[0].levelno == logging.WARNING
    assert probe_line and probe_line[0].levelno == logging.DEBUG


@pytest.mark.asyncio
async def test_retries_exhausted_raises_after_all_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt fails with a transport error; after the retry budget the
    dispatch raises ClusterOpUnreachable naming the attempt count, and the
    backoff delays follow the bounded exponential schedule (0.5, 1.0, 2.0)."""

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    captured = _patch(monkeypatch, handler=_boom)
    sleeps = _pin_retry(monkeypatch)

    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 3 attempt"):
        await cluster_rpc.dispatch_to_machine("wsl", "lifecycle", {}, retries=2)

    assert len(captured["requests"]) == 3  # 1 + 2 retries
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_delay_is_bounded_and_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw (un-jittered) schedule is bounded: 0.5 · 2**attempt, capped at
    4s — a long outage never grows the gap unboundedly."""
    from ops import cluster_rpc as cr

    monkeypatch.setattr(
        cr.random,
        "uniform",
        lambda _a, _b: 1.0,  # pyright: ignore[reportUnknownArgumentType]
    )  # pin jitter
    assert cr._retry_delay_s(0) == 0.5
    assert cr._retry_delay_s(1) == 1.0
    assert cr._retry_delay_s(2) == 2.0
    assert cr._retry_delay_s(3) == 4.0
    assert cr._retry_delay_s(10) == 4.0  # cap holds for any attempt count


@pytest.mark.asyncio
async def test_non_idempotent_kind_retries_with_stable_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn (non-idempotent) retries under an auto-generated idempotency key:
    EVERY attempt of one dispatch carries the SAME key, so the ops server can
    dedupe (replay the first run's stored outcome) instead of double-creating."""
    import json

    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("response lost after the op ran")
        return httpx.Response(200, json={"status": "completed", "result": {"id": 7}})

    captured = _patch(monkeypatch, handler=_flaky)
    _pin_retry(monkeypatch)

    result = await cluster_rpc.dispatch_to_machine(
        "wsl", "spawn-launch", {"prompt": "hi"}, retries=2
    )

    assert result == {"id": 7}
    keys = [json.loads(req.content)["idempotency_key"] for req in captured["requests"]]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert keys[0].startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_caller_supplied_idempotency_key_rides_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit idempotency_key is forwarded verbatim — a caller that
    re-dispatches the same logical op across calls passes its own key."""
    import json

    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {}}),
    )
    await cluster_rpc.dispatch_to_machine(
        "wsl", "spawn-launch", {}, idempotency_key="my-logical-op-1"
    )
    body = json.loads(captured["request"].content)
    assert body["idempotency_key"] == "my-logical-op-1"


@pytest.mark.asyncio
async def test_spawn_launch_defaults_to_its_agent_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business keys replay one effect, but never cross target/payload boundaries."""
    import json

    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {"id": 42}}),
    )

    for target, payload in (
        ("wsl", {"agent_id": 42, "name": "one"}),
        ("wsl", {"name": "one", "agent_id": 42}),
        ("linux", {"agent_id": 42, "name": "one"}),
        ("wsl", {"agent_id": 42, "name": "two"}),
    ):
        result = await cluster_rpc.dispatch_to_machine(target, "spawn-launch", payload, retries=0)
        assert result == {"id": 42}

    keys = [json.loads(request.content)["idempotency_key"] for request in captured["requests"]]
    assert keys[0] == keys[1]
    assert keys[0].startswith("spawn-launch:wsl:42:")
    assert keys[2] != keys[0]
    assert keys[3] != keys[0]


@pytest.mark.asyncio
async def test_only_spawn_launch_reuses_a_business_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed lifecycle and update retries get new dispatch keys to run again."""
    import json

    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {}}),
    )

    operations: tuple[tuple[cluster_rpc.OpKind, dict[str, Any]], ...] = (
        ("cluster_update", {"target_sha": "abc123", "mode": "smooth"}),
        ("lifecycle", {"trigger_inbound_id": 17, "action": "restart"}),
    )
    for kind, payload in operations:
        await cluster_rpc.dispatch_to_machine("wsl", kind, payload, retries=0)
        await cluster_rpc.dispatch_to_machine("wsl", kind, payload, retries=0)

    keys = [json.loads(request.content)["idempotency_key"] for request in captured["requests"]]
    assert keys[0] != keys[1]
    assert keys[2] != keys[3]
    assert keys[0].startswith("cluster_update:")
    assert keys[2].startswith("lifecycle:")


@pytest.mark.asyncio
async def test_idempotent_kind_carries_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent kinds (status_probe etc.) send no idempotency_key — no dedup
    row, no server-side storage."""
    import json

    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "completed", "result": {}}),
    )
    await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {})
    body = json.loads(captured["request"].content)
    assert body.get("idempotency_key") is None


@pytest.mark.asyncio
async def test_5xx_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 from the ops server (server mid-restart / proxy hiccup) is a
    transient infrastructure failure — retried, and a later success wins."""
    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="ops server restarting")
        return httpx.Response(200, json={"status": "completed", "result": {"paused": True}})

    captured = _patch(monkeypatch, handler=_flaky)
    _pin_retry(monkeypatch)

    result = await cluster_rpc.dispatch_to_machine("wsl", "cluster_resume", {}, retries=2)

    assert result == {"paused": True}
    assert calls["n"] == 2
    assert len(captured["requests"]) == 2  # the 503 attempt + the success


@pytest.mark.asyncio
async def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is deterministic (wrong path / wrong server) — single attempt even
    with a large retry budget; retrying cannot change the outcome."""
    captured = _patch(monkeypatch, handler=lambda _r: httpx.Response(404, text="nope"))
    _pin_retry(monkeypatch)

    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="returned 404"):
        await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {}, retries=3)

    assert len(captured["requests"]) == 1


@pytest.mark.asyncio
async def test_cluster_op_failed_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A business failure (status=failed — the op RAN and reported failure) is
    terminal; retrying a business failure is meaningless and would re-run the
    op (e.g. a second spawn)."""
    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(
            200, json={"status": "failed", "result": {"error": "boom"}}
        ),
    )
    _pin_retry(monkeypatch)

    with pytest.raises(cluster_rpc.ClusterOpFailed):
        await cluster_rpc.dispatch_to_machine("wsl", "spawn-launch", {}, retries=3)

    assert len(captured["requests"]) == 1


@pytest.mark.asyncio
async def test_malformed_response_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed response body (version skew / wrong server) is terminal —
    retrying cannot heal a contract mismatch."""
    captured = _patch(
        monkeypatch,
        handler=lambda _r: httpx.Response(200, json={"status": "pending", "result": {}}),
    )
    _pin_retry(monkeypatch)

    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"):
        await cluster_rpc.dispatch_to_machine("wsl", "spawn-launch", {}, retries=3)

    assert len(captured["requests"]) == 1


@pytest.mark.asyncio
async def test_retries_default_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `retries` uses AVA_CLUSTER_RPC_MAX_RETRIES — the retry budget is
    config, not a module literal."""
    from shared.config import settings

    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"status": "completed", "result": {}})

    _patch(monkeypatch, handler=_flaky)
    _pin_retry(monkeypatch)
    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 2)

    await cluster_rpc.dispatch_to_machine("wsl", "status_probe", {})
    assert calls["n"] == 3  # 1 + 2 configured retries
