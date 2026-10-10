"""Native alert facts, atomic fanout, recovery and SENT fences use real Postgres."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.telemetry.alerts import upsert_alert
from base.telemetry.alerts.native import load_native_group, native_sent_count, stamp_native_sent
from base.telemetry.alerts.shadow import AlertShadowBatch
from services.entrypoints.im_bridge.alert_outbound import (
    AlertAcceptanceHeldError,
    AlertOutboundBridge,
    AlertOwnerDecision,
)
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import OutboundIntent, PreparedOutboundSend
from services.entrypoints.im_bridge.outbound.worker import IMOutboxWorker
from services.entrypoints.im_bridge.tests.test_timeline_outbox import RecordingAdapter
from services.entrypoints.im_bridge.tests.test_timeline_outbox import pool as pool
from services.entrypoints.im_bridge.types import SendNotStartedError


def item(fp: str, *, status: str = "firing", severity: str = "error") -> dict[str, Any]:
    return {
        "fingerprint": fp,
        "starts_at": "2026-10-07T10:00:00Z",
        "status": status,
        "labels": {"alertname": "rule", "severity": severity},
        "annotations": {"summary": fp},
    }


def ingest(pool: ConnectionPool, items: list[dict[str, Any]], *, native: bool = True) -> set[int]:
    with pool.connection() as conn:
        batch = AlertShadowBatch(conn, items, "en", native=native)
        for observation in batch.items:
            key, previous = batch.observe(observation)
            if key is None:
                continue
            _, _, notify, row = upsert_alert(conn, observation, instance_key=key)
            batch.record(observation, row, previous, should_notify=notify)
        batch.freeze()
        return batch.native_ids


class AlertAdapter(RecordingAdapter):
    def __init__(self, channel: str = "telegram") -> None:
        super().__init__("bot-" + channel)
        self.channel = channel
        self.owner: str | None = "owner-" + channel
        self.preparations = 0
        self.recipients: list[str] = []

    async def prepare_alert_owner(self, text: str) -> tuple[str, PreparedOutboundSend]:
        self.preparations += 1
        if self.owner is None:
            raise SendNotStartedError("owner unavailable")
        return self.owner, await self.prepare_timeline(text)

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        self.recipients.append(chat_id)
        await super().send_prepared_outbound(chat_id, prepared)


def bridge(
    pool: ConnectionPool, *adapters: AlertAdapter, enabled: bool = True
) -> AlertOutboundBridge:
    return AlertOutboundBridge(
        IMOutboxStore(pool), {adapter.channel: adapter for adapter in adapters}, enabled=enabled
    )


def receipt_count(pool: ConnectionPool) -> tuple[int, int]:
    with pool.connection() as conn:
        receipt = conn.execute("SELECT count(*) FROM im_bridge_alert_acceptances").fetchone()
        intent = conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone()
        assert receipt is not None and intent is not None
        return int(receipt[0]), int(intent[0])


async def test_first_accept_freezes_subset_unavailable_and_response_loss_recovery(
    pool: ConnectionPool,
):
    [group] = ingest(pool, [item("a"), item("b")])
    ready, missing = AlertAdapter(), AlertAdapter("feishu")
    missing.owner = None
    first = await bridge(pool, ready, missing).accept(group)
    assert len(first.intent_ids) == 1
    assert [decision.decision for decision in first.decisions] == [
        AlertOwnerDecision.OWNER_UNAVAILABLE,
        AlertOwnerDecision.AVAILABLE,
    ]
    missing.owner, ready.owner, ready.account = "later-owner", "changed-owner", "changed-bot"
    recovered = await bridge(pool, missing, ready).accept(group)
    assert recovered == first
    assert ready.preparations == missing.preparations == 1
    assert receipt_count(pool) == (1, 1)
    with pool.connection() as conn:
        assert conn.execute("SELECT agent_id FROM im_bridge_outbound_intents").fetchone() == (None,)
        assert (
            conn.execute("SELECT notified_revision,notified_at FROM alerts ORDER BY id").fetchall()
            == [(0, None)] * 2
        )


async def test_whole_subset_unavailable_is_hold_without_fake_receipt_then_qualifies(
    pool: ConnectionPool,
):
    [group] = ingest(pool, [item("a")])
    adapter = AlertAdapter()
    adapter.owner = None
    service = bridge(pool, adapter)
    with pytest.raises(AlertAcceptanceHeldError):
        await service.accept(group)
    assert receipt_count(pool) == (0, 0)
    adapter.owner = "available"
    await service.accept(group)
    assert receipt_count(pool) == (1, 1)


async def test_recovery_after_committed_fact_without_rpc_or_next_webhook(pool: ConnectionPool):
    [group] = ingest(pool, [item("a")])
    adapter = AlertAdapter()
    service = bridge(pool, adapter)
    await service.poll_once()
    assert receipt_count(pool) == (1, 1)
    await IMOutboxWorker(service.store.outbound, service.adapters).run_once()
    assert adapter.sent and adapter.recipients == [adapter.owner]
    with pool.connection() as conn:
        assert native_sent_count(conn, {group}) == 1
        assert conn.execute("SELECT notified_revision FROM alerts").fetchone() == (1,)


async def test_shadow_history_mixed_unknown_fresh_and_pause_resume(pool: ConnectionPool):
    ingest(pool, [item("shadow")], native=False)
    with pool.connection() as conn:
        upsert_alert(conn, item("legacy"))
    native = ingest(pool, [item("legacy"), item("fresh"), item("shadow")])
    [group] = native
    with pool.connection() as conn:
        source = load_native_group(conn, group)
        assert len(source["members"]) == 1 and "fresh" in source["text"]
        assert conn.execute(
            "SELECT count(*) FROM alert_notification_groups WHERE origin='shadow'"
        ).fetchone() == (2,)
    adapter = AlertAdapter()
    await bridge(pool, adapter, enabled=False).poll_once()
    assert receipt_count(pool) == (0, 0)
    await bridge(pool, adapter).poll_once()
    assert receipt_count(pool) == (1, 1)
    assert ingest(pool, [item("shadow"), item("legacy")]) == set()


async def test_repeat_ab_then_abc_reuses_original_groups_and_text(pool: ConnectionPool):
    original = ingest(pool, [item("a"), item("b")])
    changed = item("a")
    changed["annotations"]["summary"] = "different same operation"
    added = ingest(pool, [changed, item("b"), item("c")])
    assert original < added
    service = bridge(pool, AlertAdapter())
    await service.poll_once()
    assert receipt_count(pool) == (2, 2)
    with pool.connection() as conn:
        original_source = load_native_group(conn, next(iter(original)))
        assert "different same operation" not in original_source["text"]
        assert conn.execute("SELECT count(*) FROM alert_notification_members").fetchone() == (3,)


async def test_rotating_scan_does_not_starve_after_more_than_page_unavailable(pool: ConnectionPool):
    for index in range(17):
        ingest(pool, [item(f"missing-{index}")])
    ingest(pool, [item("available")])
    adapter = AlertAdapter()
    original = adapter.prepare_alert_owner

    async def prepare(text: str):
        if "missing-" in text:
            raise SendNotStartedError("unavailable")
        return await original(text)

    adapter.prepare_alert_owner = prepare
    service = bridge(pool, adapter)
    await service.poll_once()
    assert receipt_count(pool) == (0, 0)
    await service.poll_once()
    assert receipt_count(pool) == (1, 1)
    await service.poll_once()
    assert receipt_count(pool) == (1, 1)


async def test_rpc_strict_source_old_path_absence_and_no_acceptance_claim(pool: ConnectionPool):
    [group] = ingest(pool, [item("a")])
    service = bridge(pool, AlertAdapter())
    for body in [
        {"group_id": True, "source_origin": "native-v1"},
        {"group_id": 2**63, "source_origin": "native-v1"},
        {"group_id": group, "source_origin": "shadow"},
        {"group_id": group, "source_origin": "native-v1", "text": "altered"},
    ]:
        assert (await service.handle(json.dumps(body).encode()))[0] == 400
    assert (await service.handle(b'{"group_id":999999,"source_origin":"native-v1"}'))[0] == 404
    assert receipt_count(pool) == (0, 0)
    status, body, _ = await service.handle(
        json.dumps({"group_id": group, "source_origin": "native-v1"}).encode()
    )
    assert status == 200 and json.loads(body)["intent_ids"]
    with pool.connection() as conn:
        assert native_sent_count(conn, {group}) == 0


async def test_any_real_sent_completes_revision_but_ambiguous_channel_is_not_replayed(
    pool: ConnectionPool,
):
    [group] = ingest(pool, [item("a")])
    good, ambiguous = AlertAdapter(), AlertAdapter("weixin")
    ambiguous.error = RuntimeError("https://provider/botSECRET/contextTOKEN")
    service = bridge(pool, good, ambiguous)
    await service.accept(group)
    worker = IMOutboxWorker(service.store.outbound, service.adapters)
    with pytest.raises(RuntimeError) as caught:
        await worker.run_once()
    assert caught.value is ambiguous.error
    await worker.run_once()
    assert len(good.sent) == len(ambiguous.sent) == 1
    with pool.connection() as conn:
        assert native_sent_count(conn, {group}) == 1
        states = conn.execute(
            "SELECT status,outcome_reason FROM im_bridge_outbound_intents ORDER BY channel"
        ).fetchall()
        assert states == [("sent", None), ("uncertain", "external_send_ambiguous")]
        serialized = str(
            conn.execute("SELECT decisions FROM im_bridge_alert_acceptances").fetchall()
        ) + str(states)
        assert "SECRET" not in serialized and "contextTOKEN" not in serialized


async def test_unresolved_sending_after_restart_stays_unconfirmed_without_second_send(
    pool: ConnectionPool,
):
    [group] = ingest(pool, [item("a")])
    adapter = AlertAdapter()
    service = bridge(pool, adapter)
    await service.accept(group)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE im_bridge_outbound_intents SET status='sending',attempt_id=%s", (uuid4(),)
        )
    await IMOutboxWorker(service.store.outbound, service.adapters).run_once()
    assert adapter.sent == []
    with pool.connection() as conn:
        assert native_sent_count(conn, {group}) == 0
        assert conn.execute("SELECT status FROM im_bridge_outbound_intents").fetchone() == (
            "sending",
        )


def test_native_completion_cas_does_not_rewrite_new_status_or_legacy_timestamp(
    pool: ConnectionPool,
):
    [firing] = ingest(pool, [item("a")])
    with pool.connection() as conn:
        conn.execute("UPDATE alerts SET notified_at='2026-10-07T10:01:00Z'")
        previous = conn.execute("SELECT notified_at FROM alerts").fetchone()
        assert previous is not None
        before = previous[0]
    [resolved] = ingest(pool, [item("a", status="resolved")])
    with pool.connection() as conn:
        assert stamp_native_sent(conn, firing) == 0
        assert native_sent_count(conn, {resolved}) == 0
        assert stamp_native_sent(conn, resolved) == 1
        assert stamp_native_sent(conn, resolved) == 0
        assert conn.execute("SELECT notified_at,notified_revision FROM alerts").fetchone() == (
            before,
            2,
        )


def test_two_connections_concurrent_accept_freezes_one_fanout(pool: ConnectionPool):
    [group] = ingest(pool, [item("a")])
    barrier = Barrier(2)

    def run(owner: str):
        adapter = AlertAdapter()
        adapter.owner = owner
        service = bridge(pool, adapter)
        source, _ = service.store.lookup(group)
        decision = asyncio.run(service._prepare("telegram", adapter, source["text"]))
        barrier.wait(timeout=5)
        return service.store.accept(source, (decision,))

    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = [executor.submit(run, owner) for owner in ("left", "right")]
        assert left.result(timeout=10) == right.result(timeout=10)
    assert receipt_count(pool) == (1, 1)


async def test_retained_receipt_recovers_after_source_retention_without_new_prepare(
    pool: ConnectionPool,
):
    [group] = ingest(pool, [item("a")])
    adapter = AlertAdapter()
    service = bridge(pool, adapter)
    accepted = await service.accept(group)
    with pool.connection() as conn:
        conn.execute("DELETE FROM alert_notification_members WHERE group_id=%s", (group,))
        conn.execute("DELETE FROM alert_notification_groups WHERE id=%s", (group,))
    adapter.owner, adapter.account = "changed", "changed-account"
    assert await bridge(pool, adapter, enabled=False).accept(group) == accepted
    assert adapter.preparations == 1 and receipt_count(pool) == (1, 1)


async def test_acceptance_insert_failure_rolls_back_all_intents_and_receipt(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    [group] = ingest(pool, [item("a")])
    service = bridge(pool, AlertAdapter(), AlertAdapter("weixin"))
    original = service.store.outbound.insert_intent
    count = 0

    def fail_second(conn: Connection, intent: OutboundIntent | None) -> int:
        nonlocal count
        count += 1
        result = original(conn, intent)
        if count == 2:
            raise RuntimeError("injected after intent writes")
        return result

    monkeypatch.setattr(service.store.outbound, "insert_intent", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        await service.accept(group)
    assert receipt_count(pool) == (0, 0)


async def test_actual_daemon_rpc_auth_and_new_path_does_not_claim_legacy_success(
    pool: ConnectionPool,
):
    from hashlib import sha256

    from base.daemon.health import start_health_server, stop_health_server
    from base.daemon.tests.health_support import unknown_image

    [group] = ingest(pool, [item("a")])
    service = bridge(pool, AlertAdapter())
    server = await start_health_server(
        "im_bridge",
        0,
        extra_routes={("POST", "/send/alert-outbound-v1"): service.handle},
        auth_digests=frozenset({sha256(b"mock-api-token").hexdigest()}),
        image=unknown_image(),
    )
    try:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            payload = {"group_id": group, "source_origin": "native-v1"}
            assert (await client.post("/send/alert-outbound-v1", json=payload)).status_code == 401
            auth = {"Authorization": "Bearer mock-api-token"}
            assert (await client.post("/send", json=payload, headers=auth)).status_code == 404
            response = await client.post("/send/alert-outbound-v1", json=payload, headers=auth)
            assert response.status_code == 200 and response.json()["intent_ids"]
        with pool.connection() as conn:
            assert native_sent_count(conn, {group}) == 0
    finally:
        await stop_health_server(server)


async def test_inverse_commit_order_recovery_does_not_skip_lower_id(pool: ConnectionPool):
    service = bridge(pool, AlertAdapter())
    with pool.connection() as low_conn:
        observation = item("low")
        batch = AlertShadowBatch(low_conn, [observation], "en", native=True)
        key, previous = batch.observe(observation)
        assert key is not None
        _, _, notify, row = upsert_alert(low_conn, observation, instance_key=key)
        batch.record(observation, row, previous, should_notify=notify)
        batch.freeze()
        [low] = batch.native_ids
        [high] = ingest(pool, [item("high")])
        assert low < high
        await service.poll_once()
        assert receipt_count(pool) == (1, 1)
        low_conn.commit()
    await service.poll_once()
    assert receipt_count(pool) == (2, 2)


async def test_source_change_before_commit_conflicts_without_effect(pool: ConnectionPool):
    from services.entrypoints.im_bridge.outbound.types import OutboundIdentityConflictError

    [group] = ingest(pool, [item("a")])
    service = bridge(pool, AlertAdapter())
    source, _ = service.store.lookup(group)
    decision = await service._prepare("telegram", service.adapters["telegram"], source["text"])
    with pool.connection() as conn:
        conn.execute("UPDATE alert_notification_groups SET text='changed' WHERE id=%s", (group,))
    with pytest.raises(OutboundIdentityConflictError):
        service.store.accept(source, (decision,))
    assert receipt_count(pool) == (0, 0)
