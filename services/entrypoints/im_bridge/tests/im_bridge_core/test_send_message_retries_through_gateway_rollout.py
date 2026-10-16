from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from services.entrypoints.im_bridge import copy, push_watchdog
from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.slices import gateway_client, im_bridge_config
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    FakeFailingWeixinAdapter,
    FakeFlakyWeixinAdapter,
    FakeGateway,
    FakePlainAdapter,
    FakeTypingAdapter,
    assert_effort_layer,
    assert_model_layer,
    create_test_core,
    models_data,
    reply_text,
    row,
)
from services.entrypoints.im_bridge.types import (
    ChatState,
    InboundMessage,
    Reply,
    RetryableTransportError,
)


def test_send_message_retries_through_gateway_rollout() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": "restarting"})
        return httpx.Response(201, json={"status": "delivered"})

    async def scenario() -> None:
        client = gateway_client(im_bridge_config(im_send_retry_delays=(0.01, 0.01, 0.01)))
        client._client = httpx.AsyncClient(
            base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
        )
        await client.send_message(405, "hi")

    asyncio.run(scenario())
    assert len(calls) == 3
    assert calls[0].url.path == "/api/agents/405/messages"


def test_send_message_gives_up_after_retries() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"detail": "restarting"})

    async def scenario() -> None:
        client = gateway_client(im_bridge_config(im_send_retry_delays=(0.01, 0.01)))
        client._client = httpx.AsyncClient(
            base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            await client.send_message(405, "hi")

    asyncio.run(scenario())
    assert len(calls) == 2


async def test_list_on_button_channel_is_text_one_liner_plus_buttons() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(
            agents=[
                row(405, label="Ava \u8d1f\u8d23\u4eba"),
                row(228, label=None, status="running"),
                row(999, label="gone", status="terminated"),
            ]
        )
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        out = await core._cmd_list("telegram")
        assert isinstance(out, Reply)
        assert out.text == copy.LIVE_AGENTS_TITLE_BUTTONS
        assert out.buttons is not None
        labels = [b[0] for b in out.buttons]
        cmds = [b[1] for b in out.buttons]
        assert labels == [
            f"228 {copy.UNNAMED_LABEL} [running]",
            "405 Ava \u8d1f\u8d23\u4eba [idling]",
        ]
        assert cmds == ["/switch 228", "/switch 405"]


async def test_list_on_plain_channel_keeps_full_text_list() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(agents=[row(405, label="Ava \u8d1f\u8d23\u4eba")])
        core = create_test_core(gateway, tasks=_owned_tasks)
        core.register(FakePlainAdapter())
        out = await core._cmd_list("weixin")
        assert isinstance(out, Reply)
        assert "405  Ava \u8d1f\u8d23\u4eba  [idling]" in out.text
        assert out.buttons is None


async def test_chat_typing_starts_and_stops_on_agent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge import core as core_mod

        monkeypatch.setattr(core_mod, "TYPING_INTERVAL_S", 0.01)
        monkeypatch.setattr(core_mod, "TYPING_MAX_S", 60.0)
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405

        async def scenario() -> None:
            core.outbound_store.accept(
                state.channel, "test-account", state.chat_id, 405, [], replay_id="initial-switch"
            )
            out = await core._handle_chat(state, "hi")
            assert out is None
            assert gateway.sent == [(405, "hi", "user")]
            await asyncio.sleep(0.06)
            calls_before = len(adapter.typing_calls)
            assert calls_before >= 3
            gateway.timeline = [
                {
                    "kind": "agent_chat",
                    "item_id": "6.0",
                    "payload": "answer",
                    "source_message_id": "stored-answer",
                    "source_block_idx": 0,
                }
            ]
            await core._push_snapshot(("telegram", "12345"), state, {"items": []})
            calls_after_acceptance = len(adapter.typing_calls)
            assert ("telegram", "12345") not in core._typing_tasks
            await asyncio.sleep(0.06)
            assert len(adapter.typing_calls) == calls_after_acceptance
            await core.outbound_worker.run_once()
            assert adapter.sent == [("12345", "[Ava #405] answer")]

        await scenario()


async def test_typing_skipped_for_plain_adapters() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakePlainAdapter()
        core.register(adapter)
        state = ChatState("weixin", "67890")
        state.current_agent_id = 405

        async def scenario() -> None:
            core.outbound_store.accept(
                state.channel, "test-account", state.chat_id, 405, [], replay_id="initial-switch"
            )
            await core._handle_chat(state, "hi")
            await asyncio.sleep(0.02)
            assert adapter.typing_calls == []
            gateway.timeline = [
                {
                    "kind": "agent_chat",
                    "item_id": "6.0",
                    "payload": "answer",
                    "source_message_id": "stored-answer",
                    "source_block_idx": 0,
                }
            ]
            await core._push_snapshot(("weixin", "67890"), state, {"items": []})
            await core.outbound_worker.run_once()
            assert adapter.sent == [("67890", "[Ava #405] answer")]

        await scenario()


async def test_one_typing_loop_per_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge import core as core_mod

        monkeypatch.setattr(core_mod, "TYPING_INTERVAL_S", 0.01)
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405

        async def scenario() -> None:
            await core._handle_chat(state, "first")
            await asyncio.sleep(0.02)
            calls_after_first = len(adapter.typing_calls)
            await core._handle_chat(state, "second")
            await asyncio.sleep(0.02)
            assert len(adapter.typing_calls) <= calls_after_first + 3
            assert gateway.sent == [(405, "first", "user"), (405, "second", "user")]

        await scenario()


async def test_typing_gives_up_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge import core as core_mod

        monkeypatch.setattr(core_mod, "TYPING_INTERVAL_S", 0.01)
        monkeypatch.setattr(core_mod, "TYPING_MAX_S", 0.05)
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405

        async def scenario() -> None:
            await core._handle_chat(state, "hi")
            await asyncio.sleep(0.15)
            assert core._typing_tasks == {}
            assert len(adapter.typing_calls) >= 1

        await scenario()


async def test_spawn_menu_layers_render_with_summary_button() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(models=models_data())
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")

        assert_model_layer(await core._cmd_spawn(state))
        assert_effort_layer(await core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro"))


async def test_spawn_menu_effort_fallback_without_model_options() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(models=models_data())
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")
        await core._cmd_spawn(state)
        out = await core._handle_spawn_menu(state, "spawn:model:deepseek-v4-flash")
        assert isinstance(out, Reply)
        labels = [b[0] for b in out.buttons or []]
        assert labels[1] == "effort: low"
        assert labels[-2] == "effort: max"
        assert "effort: medium" in labels


async def test_spawn_menu_every_layer_completable() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(models=models_data())
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")

        await core._cmd_spawn(state)
        out = await core._handle_spawn_menu(state, "spawn:go")
        assert isinstance(out, Reply)
        assert gateway.spawned == [{}]
        assert copy.SPAWNED_PLAIN.format(agent_id=777) in out.text
        assert out.buttons == [(copy.SPAWN_SWITCH_BUTTON.format(agent_id=777), "/switch 777")]

        await core._cmd_spawn(state)
        await core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro")
        await core._handle_spawn_menu(state, "spawn:effort:max")
        out2 = await core._handle_spawn_menu(state, "spawn:go")
        assert isinstance(out2, Reply)
        assert gateway.spawned[-1] == {"llm_model": "deepseek-v4-pro", "reasoning_effort": "max"}
        assert copy.SPAWNED_PLAIN.format(agent_id=777) in out2.text


async def test_spawn_menu_provider_default_effort_means_unset() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(models=models_data())
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")
        await core._cmd_spawn(state)
        await core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro")
        await core._handle_spawn_menu(state, "spawn:effort:")
        await core._handle_spawn_menu(state, "spawn:go")
        assert gateway.spawned[-1] == {"llm_model": "deepseek-v4-pro"}


async def test_commands_lists_everything() -> None:
    async with owned_tasks() as _owned_tasks:
        core = create_test_core(FakeGateway(), tasks=_owned_tasks)
        out = core._cmd_help()
        text = reply_text(out)
        assert "/list" in text and "/spawn" in text
        assert "/status" in text and "/commands" in text
        assert "/switch" not in text


async def test_commands_lists_ava_slash_catalog_as_buttons() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        gateway.commands = [
            {
                "name": "audio-transcribe",
                "description": "Transcribe local audio/video files, YouTube videos, or media URLs to plain text via OpenAI",
                "instruction_hint": "<src>",
            },
            {
                "name": "ava-fleet",
                "description": "Decompose a large goal into parallel workers",
                "instruction_hint": "<goal>",
            },
        ]
        core = create_test_core(gateway, tasks=_owned_tasks)
        core.register(FakeTypingAdapter())
        out = await core._cmd_commands("telegram")
        assert isinstance(out, Reply)
        assert "/audio-transcribe" in out.text
        assert "/ava-fleet" in out.text
        assert copy.COMMANDS_HEADER in out.text
        assert copy.COMMANDS_INTRO.format(count=2) in out.text
        assert "—" not in out.text
        assert "…" not in out.text
        assert "media URLs" not in out.text
        assert "or me..." in out.text
        assert out.buttons is not None
        assert ("/audio-transcribe", "/audio-transcribe") in out.buttons
        assert ("/ava-fleet", "/ava-fleet") in out.buttons


async def test_commands_plain_channel_keeps_text_only() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        core.register(FakePlainAdapter())
        out = await core._cmd_commands("weixin")
        assert isinstance(out, Reply)
        assert "/audio-transcribe" in out.text
        assert out.buttons is None


async def test_commands_empty_catalog() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        gateway.commands = []
        core = create_test_core(gateway, tasks=_owned_tasks)
        out = await core._cmd_commands("telegram")
        assert reply_text(out) == copy.NO_COMMANDS_REGISTERED


async def test_unknown_slash_command_forwards_to_current_agent() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405
        out = await core._handle_command(state, "/audio-transcribe go")
        assert out is None
        assert gateway.sent == [(405, "/audio-transcribe go", "user")]


async def test_unknown_slash_command_without_agent_errors() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")
        out = await core._handle_command(state, "/audio-transcribe go")
        assert out is not None
        assert copy.NO_AGENT_SWITCHED in reply_text(out)
        assert gateway.sent == []


async def test_weixin_push_failure_emits_the_failed_event(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        wx = FakeFailingWeixinAdapter()
        core.register(wx)
        wx.push_failures = 2
        wx.push_failed_at = 1234.0
        sleeps: list[float] = []

        async def _record(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(push_watchdog, "push_watchdog_sleep", _record)

        await core._send("weixin", "o9cq804", Reply("hello"))

        assert wx.send_attempts == 1
        assert sleeps == []
        events = [r["extra"] for r in loguru_records if r["extra"].get("event") == "im_push_failed"]
        assert [(e["channel"], e["failures"]) for e in events] == [("weixin", 2)]


async def test_weixin_healed_retry_emits_no_failed_event(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        wx = FakeFlakyWeixinAdapter([])
        core.register(wx)

        async def _no_sleep(seconds: float) -> None:
            del seconds

        monkeypatch.setattr(push_watchdog, "push_watchdog_sleep", _no_sleep)
        await core._send("weixin", "o9cq804", Reply("hello"))
        assert not [r for r in loguru_records if r["extra"].get("event") == "im_push_failed"]


async def test_weixin_push_recovery_hints_on_next_inbound() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        wx = FakeFailingWeixinAdapter()
        core.register(wx)
        import time as _time

        wx.push_failures = 0
        wx.push_recovered_at = _time.time()
        wx.push_failed_at = _time.time() - 5

        async def ok_send(
            chat_id: str,
            text: str,
            *,
            buttons: list[tuple[str, str]] | None = None,
            markdown: bool = False,
        ) -> None:
            del buttons, markdown
            wx.sent.append((chat_id, text))

        wx.send = ok_send
        from services.entrypoints.im_bridge.types import InboundMessage

        async def scenario() -> None:
            await core.handle_inbound(
                InboundMessage(channel="weixin", chat_id="o9cq804", text="\u5728", message_id="m1")
            )
            texts = [t for _, t in wx.sent]
            assert any("push link failed earlier and has now recovered" in t for t in texts)

        await scenario()


async def test_push_retry_sleeps_backoff_before_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        events: list[str] = []

        async def _sleep(seconds: float) -> None:
            base = core.config.im_push_retry_backoff_seconds
            jitter = core.config.im_push_retry_jitter_seconds
            assert base <= seconds <= base + jitter
            events.append("sleep")

        monkeypatch.setattr(push_watchdog, "push_watchdog_sleep", _sleep)
        wx = FakeFlakyWeixinAdapter(events)
        tg = FakeTypingAdapter()
        core.register(wx)
        core.register(tg)

        async def scenario() -> None:
            await core._send("weixin", "o9cq804", Reply("hello"))

        await scenario()
        assert events == ["send", "sleep", "send"]
        assert wx.sent == [("o9cq804", "hello")]
        assert tg.owner_sent == []
        assert wx.push_failures == 0
        assert wx.push_recovered_at is not None


def test_push_retry_backoff_is_config_backed(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = im_bridge_config()
    assert defaults.im_push_retry_backoff_seconds == 1.0
    assert defaults.im_push_retry_jitter_seconds == 2.0
    fixed = im_bridge_config(im_push_retry_backoff_seconds=1.5, im_push_retry_jitter_seconds=0.0)
    assert push_watchdog.retry_backoff_seconds(fixed) == 1.5
    jittery = im_bridge_config(im_push_retry_backoff_seconds=1.5, im_push_retry_jitter_seconds=2.0)
    delays = [push_watchdog.retry_backoff_seconds(jittery) for _ in range(50)]
    assert all(1.5 <= delay <= 3.5 for delay in delays)
    assert len(set(delays)) > 1


async def test_restore_subscriptions_skips_disabled_channels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    async with owned_tasks() as _owned_tasks:
        monkeypatch.setenv("AVA_HOME", str(tmp_path))
        gateway = FakeGateway(agents=[row(405, label="Ava \u8d1f\u8d23\u4eba")])
        core = create_test_core(gateway, im_disabled_adapters=(), tasks=_owned_tasks)
        for channel, chat_id in (("weixin", "wx123"), ("telegram", "12345")):
            state = ChatState(channel, chat_id)
            await core._cmd_switch(state, "405")
            assert (channel, chat_id) in core._subscriptions

        core2 = create_test_core(
            FakeGateway(agents=[]), im_disabled_adapters=("weixin",), tasks=_owned_tasks
        )
        assert core2._disabled_channels == {"weixin"}
        await core2.restore_subscriptions()
        assert ("weixin", "wx123") not in core2._subscriptions
        assert ("telegram", "12345") in core2._subscriptions


async def test_push_snapshot_watermark_compares_numerically() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway()
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405

        def snapshot(item_id: str, payload: str) -> dict[str, Any]:
            return {"items": [{"item_id": item_id, "kind": "agent_chat", "payload": payload}]}

        async def scenario() -> None:
            core.cursor_store.save_push("telegram", "12345", 405, PushWatermark(None, "9.5"))
            gateway.timeline = [
                dict(item, source_message_id="stored-ten", source_block_idx=0)
                for item in snapshot("10.1", "ten")["items"]
            ]
            await core._push_snapshot(("telegram", "12345"), state, {})
            await core.outbound_worker.run_once()
            assert adapter.sent == [("12345", "[Ava #405] ten")]
            assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(None, "10.1")
            gateway.timeline = [
                dict(item, source_message_id="stored-nine", source_block_idx=0)
                for item in snapshot("9.9", "nine")["items"]
            ]
            await core._push_snapshot(("telegram", "12345"), state, {})
            await core.outbound_worker.run_once()
            assert adapter.sent == [("12345", "[Ava #405] ten")]

        await scenario()


async def test_sse_reconnect_log_does_not_kill_subscription_loop() -> None:
    async with owned_tasks() as _owned_tasks:
        gateway = FakeGateway(stream_failures=1)
        core = create_test_core(gateway, tasks=_owned_tasks)
        state = ChatState("telegram", "12345")
        state.current_agent_id = 405

        async def scenario() -> None:
            task = asyncio.create_task(core._subscription_loop(("telegram", "12345"), state))
            await asyncio.sleep(0.3)
            assert not task.done(), "the reconnect log call killed the loop"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await scenario()


async def test_handle_inbound_reports_unconfirmed_delivery_when_gateway_stays_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    async with owned_tasks() as _owned_tasks:
        monkeypatch.setenv("AVA_HOME", str(tmp_path))
        gateway = FakeGateway(send_failures=10)
        core = create_test_core(gateway, tasks=_owned_tasks)
        adapter = FakeTypingAdapter()
        core.register(adapter)
        state = core._get_or_create_state("telegram", "12345")
        state.current_agent_id = 405
        msg = InboundMessage(channel="telegram", chat_id="12345", text="hello")

        async def scenario() -> None:
            await core.handle_inbound(msg)
            assert gateway.sent == []
            assert adapter.sent == [("12345", copy.DELIVERY_UNCONFIRMED_NOTICE)]
            assert not any(
                word in copy.DELIVERY_UNCONFIRMED_NOTICE.lower() for word in ("queue", "retry")
            )
            assert list(tmp_path.rglob("outbox*")) == []
            await asyncio.sleep(0)
            assert gateway.sent == []

        await scenario()


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_only_declared_transient_statuses_retry_with_the_same_identity(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status if len(calls) == 1 else 201, json={"status": "delivered"})

    client = gateway_client(im_bridge_config(im_send_retry_delays=(0, 0)))
    async with httpx.AsyncClient(
        base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
    ) as http:
        client._client = http
        await client.send_message(405, "hi", idempotency_key="stable-message")
    assert len(calls) == 2
    assert [call.headers["Idempotency-Key"] for call in calls] == [
        "stable-message",
        "stable-message",
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_nontransient_status_is_visible_without_retry(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "invalid request"})

    client = gateway_client(im_bridge_config(im_send_retry_delays=(0, 0)))
    async with httpx.AsyncClient(
        base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
    ) as http:
        client._client = http
        with pytest.raises(RuntimeError) as caught:
            await client.send_message(405, "hi")
    assert not isinstance(caught.value, RetryableTransportError)
    assert len(calls) == 1
