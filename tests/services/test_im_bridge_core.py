"""`services.im_bridge.core` command routing against the real gateway shape.

Regression guard: GET /api/agents rows carry ``agent_id`` (not ``id``) — the
first field-name mismatch made ``/list`` crash with KeyError('id') in prod
(2026-08-03). Every fake below uses the real gateway row shape, so a revert
to ``a["id"]`` fails immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from services.im_bridge import copy
from services.im_bridge import state as state_mod
from services.im_bridge.core import IMBridgeCore
from services.im_bridge.types import ChatState, IMAdapter, InboundMessage, Reply
from shared.config import settings


def _row(
    agent_id: int,
    *,
    label: str | None = None,
    status: str = "idling",
) -> dict[str, Any]:
    """One GET /api/agents row in the real gateway shape."""
    return {
        "agent_id": agent_id,
        "label": label,
        "status": status,
        "spawner": "user",
        "machine": "gateway-host",
        "spawned_at": "2026-08-01T00:00:00+00:00",
        "started_at": None,
        "last_active_at": None,
        "pid": None,
    }


class FakeGateway:
    """GatewayClient stand-in returning the real response shapes."""

    def __init__(
        self,
        agents: list[dict[str, Any]] | None = None,
        timeline: list[dict[str, Any]] | None = None,
        presets: list[dict[str, Any]] | None = None,
        models: dict[str, Any] | None = None,
        *,
        send_failures: int = 0,
        stream_failures: int = 0,
    ) -> None:
        self.agents = agents or []
        self.timeline = timeline or []
        self.presets = presets or []
        self.models = models or {"models": {}, "default": "deepseek-v4-pro"}
        self.commands: list[dict[str, Any]] | None = None
        self.sent: list[tuple[int, str, str]] = []
        self.sent_keys: list[str | None] = []
        self.spawned: list[tuple[str | None, dict[str, object] | None]] = []
        self.send_failures = send_failures
        self.stream_failures = stream_failures

    async def list_agents(self) -> list[dict[str, Any]]:
        return self.agents

    async def list_commands(self) -> list[dict[str, Any]]:
        commands = getattr(self, "commands", None)
        if commands is not None:
            return commands
        return [
            {
                "name": "audio-transcribe",
                "description": "Transcribe audio/video to text",
                "instruction_hint": "<audio|video|url>",
            },
            {
                "name": "ava-fleet",
                "description": "Decompose a large goal into parallel workers",
                "instruction_hint": "<goal>",
            },
        ]

    async def list_presets(self) -> list[dict[str, Any]]:
        return self.presets

    async def list_models(self) -> dict[str, Any]:
        return self.models

    async def spawn_agent(self, *, preset: str | None, config: dict[str, object] | None) -> int:
        self.spawned.append((preset, config))
        return 777

    async def get_timeline(self, agent_id: int, limit: int = 5) -> list[dict[str, Any]]:
        return self.timeline

    async def send_message(
        self,
        agent_id: int,
        text: str,
        source: str = "user",
        *,
        idempotency_key: str | None = None,
    ) -> None:
        if self.send_failures > 0:
            self.send_failures -= 1
            raise RuntimeError("gateway down")
        self.sent.append((agent_id, text, source))
        self.sent_keys.append(idempotency_key)

    async def stream_events(self, agent_id: int) -> Any:
        if self.stream_failures > 0:
            self.stream_failures -= 1
            raise RuntimeError("stream down")
        # Park forever — the subscription task is cancelled at test teardown.
        await asyncio.Event().wait()
        yield None  # pragma: no cover - unreachable


def _core(gateway: FakeGateway) -> IMBridgeCore:
    core = IMBridgeCore()
    core.gateway = gateway  # type: ignore[assignment]
    return core


def _text(reply: object) -> str:
    """Flatten a Reply or list[Reply] into one joined string."""

    replies = reply if isinstance(reply, list) else [reply]
    return "\n".join(r.text for r in replies if r is not None)  # type: ignore[union-attr]


def test_cmd_list_renders_agent_id() -> None:
    """/list renders rows from the real shape — regression for KeyError('id')."""
    gateway = FakeGateway(
        agents=[
            _row(405, label="Ava \u8d1f\u8d23\u4eba"),
            _row(228, label=None, status="running"),
            _row(999, label="gone", status="terminated"),  # filtered out
        ]
    )
    # no adapter registered -> plain text-list path (the button path is
    # covered in the v3 section with a button-capable adapter)
    out = asyncio.run(_core(gateway)._cmd_list("telegram"))
    assert isinstance(out, Reply)
    text = _text(out)
    assert "405  Ava \u8d1f\u8d23\u4eba  [idling]" in text
    assert f"228  {copy.UNNAMED_LABEL}  [running]" in text
    assert "999" not in text
    assert copy.LIVE_AGENTS_TITLE in text
    assert out.buttons is None


def test_cmd_list_no_alive_agents() -> None:
    gateway = FakeGateway(agents=[_row(1, status="terminated")])
    out = asyncio.run(_core(gateway)._cmd_list("telegram"))
    assert _text(out) == copy.NO_LIVE_AGENTS


def test_cmd_switch_matches_agent_id() -> None:
    """/switch 405 selects the row by agent_id and starts the subscription."""
    gateway = FakeGateway(
        agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")],
        timeline=[
            {"kind": "agent_chat", "item_id": "3.1", "payload": "hello"},
        ],
    )
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "405"))
    text = _text(out)
    assert state.current_agent_id == 405
    assert copy.SWITCHED_TO.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in text
    assert "hello" in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == "3.1"


def test_cmd_switch_matches_label() -> None:
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "ava \u8d1f\u8d23\u4eba"))
    assert state.current_agent_id == 405
    assert copy.SWITCHED_TO.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in _text(out)


def test_cmd_switch_replays_five_dialog_items_amid_non_dialog() -> None:
    """/switch replays the most recent 5 dialog messages even when the raw
    timeline mixes in non-dialog items (agent_updated etc.) — the old
    limit=5 on raw items could yield as few as 2 messages (user feedback
    2026-08-05: "\u53ea\u63a8\u9001\u6700\u8fd1 2 \u6761\u592a\u5c11\u4e86")."""
    gateway = FakeGateway(
        agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")],
        timeline=[
            {"kind": "agent_updated", "item_id": "1.0"},
            {"kind": "agent_chat", "item_id": "1.1", "payload": "m1"},
            {"kind": "agent_chat", "item_id": "2.1", "payload": "m2"},
            {"kind": "agent_updated", "item_id": "3.0"},
            {"kind": "agent_chat", "item_id": "3.1", "payload": "m3"},
            {"kind": "agent_chat", "item_id": "4.1", "payload": "m4"},
            {"kind": "agent_chat", "item_id": "5.1", "payload": "m5"},
        ],
    )
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "405"))
    text = _text(out)
    for m in ("m1", "m2", "m3", "m4", "m5"):
        assert m in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == "5.1"


def test_cmd_switch_replay_caps_at_five() -> None:
    """More than 5 dialog messages -> only the most recent 5 are replayed."""
    gateway = FakeGateway(
        agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")],
        timeline=[
            {"kind": "agent_chat", "item_id": f"{i}.1", "payload": f"m{i}"} for i in range(1, 9)
        ],
    )
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "405"))
    text = _text(out)
    for m in ("m1", "m2", "m3"):
        assert m not in text
    for m in ("m4", "m5", "m6", "m7", "m8"):
        assert m in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == "8.1"


def test_cmd_switch_unknown_agent() -> None:
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "999"))
    assert state.current_agent_id is None
    assert copy.AGENT_NOT_FOUND.format(arg="999") in _text(out)


def test_cmd_status_reads_agent_id() -> None:
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba", status="running")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    out = asyncio.run(core._cmd_status(state))
    text = _text(out)
    assert copy.STATUS_DETAIL_LINE.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in text
    assert copy.STATUS_STATE_LINE.format(status="running") in text


def test_cmd_status_clears_vanished_agent() -> None:
    gateway = FakeGateway(agents=[])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    out = asyncio.run(core._cmd_status(state))
    assert state.current_agent_id is None
    assert copy.CURRENT_AGENT_GONE in _text(out)


def test_chat_without_switch_errors() -> None:
    gateway = FakeGateway()
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._handle_chat(state, "hi"))
    assert out is not None
    assert copy.NO_AGENT_SWITCHED in _text(out)
    assert gateway.sent == []


def test_chat_forwards_to_current_agent() -> None:
    gateway = FakeGateway()
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    out = asyncio.run(core._handle_chat(state, "hi"))
    assert out is None
    # IM is a frontend — the human through any channel is plain "user"
    # (source whitelist: system / agent:N / user / ui:page:<name> / ...).
    assert gateway.sent == [(405, "hi", "user")]


# --- v2: switch semantics / persistence / filtering / rendering ---


def test_switch_without_arg_is_usage_error() -> None:
    """/switch with no argument is an error — the picker lives on /list's
    tap-to-switch card, not here (user ruling 2026-08-03)."""
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, ""))
    assert isinstance(out, Reply)
    assert copy.SWITCH_USAGE in out.text
    assert out.buttons is None
    assert state.current_agent_id is None  # an error never switches


def test_restore_subscriptions_rebuilds_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A fresh core (daemon restart) rebuilds SSE subscriptions from the
    persisted switch_state — agent replies must flow again without the user
    re-running /switch (Task #804)."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_switch(state, "405"))
    assert core._subscriptions  # subscription created by the switch

    # a fresh core simulating daemon restart: switch_state restored, but the
    # in-memory subscription is gone — restore_subscriptions() rebuilds it
    core2 = _core(FakeGateway(agents=[]))
    assert core2._subscriptions == {}
    asyncio.run(core2.restore_subscriptions())
    assert ("telegram", "12345") in core2._subscriptions
    assert core2._subscriptions[("telegram", "12345")] is not None


def test_handle_chat_ensures_subscription(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Sending a chat message must (re)create the push subscription even if
    it was lost (e.g. daemon restarted since the last /switch)."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway()
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    asyncio.run(core._handle_chat(state, "hi"))
    assert ("telegram", "12345") in core._subscriptions
    # and it stays a single subscription on the next message
    asyncio.run(core._handle_chat(state, "again"))
    assert len(core._subscriptions) == 1


def test_switch_persists_across_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The switched agent survives a core restart: state is read back from
    the switch_state file when the chat is next seen."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_switch(state, "405"))
    assert state.current_agent_id == 405
    assert (tmp_path / "state" / "im_bridge" / "switch_state.json").exists()

    # a fresh core (simulating daemon restart) restores the binding
    core2 = _core(FakeGateway(agents=[]))
    state2 = core2._get_or_create_state("telegram", "12345")
    assert state2.current_agent_id == 405


def test_switch_state_cleared_when_agent_vanishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_switch(state, "405"))
    # agent disappears; /status clears and persists the clearing
    core2 = _core(FakeGateway(agents=[]))
    state2 = core2._get_or_create_state("telegram", "12345")
    out = asyncio.run(core2._cmd_status(state2))
    assert state2.current_agent_id is None
    assert copy.CURRENT_AGENT_GONE in _text(out)
    core3 = _core(FakeGateway(agents=[]))
    assert core3._get_or_create_state("telegram", "12345").current_agent_id is None


def test_dialog_filter_keeps_only_user_and_agent_text() -> None:
    """Push filter: user inbound + agent text only; peer-agent inbound,
    code, reasoning and system rows are all dropped."""
    from services.im_bridge.core import _is_dialog_item

    items = [
        {"kind": "inbound_chat", "source": "user", "payload": "hi"},
        {"kind": "inbound_chat", "source": "agent:1818", "payload": "peer msg"},
        {"kind": "inbound_chat", "source": "watcher:3", "payload": "wake"},
        {"kind": "agent_chat", "payload": "answer"},
        {"kind": "agent_code", "payload": "code"},
        {"kind": "code_output", "payload": "out"},
        {"kind": "agent_reasoning", "payload": "think"},
        {"kind": "system_prompt", "payload": "sys"},
    ]
    kept = [it["kind"] + ":" + str(it.get("source")) for it in items if _is_dialog_item(it)]
    assert kept == ["inbound_chat:user", "agent_chat:None"]


def test_switch_summary_uses_strict_filter() -> None:
    """Recent-messages summary shows only user+agent text — code/output rows
    from the same window are not echoed."""
    gateway = FakeGateway(
        agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")],
        timeline=[
            {"kind": "inbound_chat", "source": "agent:1818", "item_id": "1.0", "payload": "peer"},
            {"kind": "agent_chat", "item_id": "2.0", "payload": "real answer"},
            {"kind": "agent_code", "item_id": "3.0", "payload": "print(1)"},
            {"kind": "code_output", "item_id": "4.0", "payload": "1"},
        ],
    )
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "405"))
    text = _text(out)
    assert "real answer" in text
    assert "peer" not in text
    assert "print(1)" not in text
    # one message per item: header + 1 kept item
    assert isinstance(out, list) and len(out) == 2


def test_render_item_tags_speaker() -> None:
    """Pushed lines carry the [User] / [Ava #<id>] speaker tag."""
    from services.im_bridge.core import _render_item

    assert _render_item({"kind": "inbound_chat", "payload": "hi"}, 405) == "[User] hi"
    assert _render_item({"kind": "agent_chat", "payload": "answer"}, 405) == "[Ava #405] answer"


def test_send_message_retries_through_gateway_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx from the gateway (mid-rollout) is retried with backoff until it
    lands — an IM message must not be dropped because the gateway blinked."""
    from services.im_bridge.gateway_client import GatewayClient

    monkeypatch.setattr(settings.services, "im_send_retry_delays", [0.01, 0.01, 0.01])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": "restarting"})
        return httpx.Response(201, json={"status": "delivered"})

    async def scenario() -> None:
        client = GatewayClient()
        client._base = "http://localhost:8000"
        client._client = httpx.AsyncClient(
            base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
        )
        await client.send_message(405, "hi")

    asyncio.run(scenario())
    assert len(calls) == 3
    assert calls[0].url.path == "/api/agents/405/messages"


def test_send_message_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.im_bridge.gateway_client import GatewayClient

    monkeypatch.setattr(settings.services, "im_send_retry_delays", [0.01, 0.01])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"detail": "restarting"})

    async def scenario() -> None:
        client = GatewayClient()
        client._base = "http://localhost:8000"
        client._client = httpx.AsyncClient(
            base_url="http://localhost:8000", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            await client.send_message(405, "hi")

    asyncio.run(scenario())
    assert len(calls) == 2


# --- v3: typing indicator + button-capable /list (Telegram) ---


class FakeTypingAdapter(IMAdapter):
    """Adapter stand-in mirroring the Telegram adapter's button + typing
    contract: records sends/typing calls, never fails."""

    channel = "telegram"
    can_buttons = True
    can_type = True

    def __init__(self) -> None:
        super().__init__(core=None)  # type: ignore[arg-type]
        self.sent: list[tuple[str, str]] = []
        self.typing_calls: list[str] = []
        self.owner_sent: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        self.sent.append((chat_id, text))

    async def typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        del markdown
        self.owner_sent.append(text)


class FakePlainAdapter(FakeTypingAdapter):
    """WeChat/Feishu: same transport contract, no buttons, no typing."""

    channel = "weixin"
    can_buttons = False
    can_type = False


def test_list_on_button_channel_is_text_one_liner_plus_buttons() -> None:
    """/list on Telegram: the text is one line only — id/label/status all
    live on the buttons (user ruling 2026-08-03)."""
    gateway = FakeGateway(
        agents=[
            _row(405, label="Ava \u8d1f\u8d23\u4eba"),
            _row(228, label=None, status="running"),
            _row(999, label="gone", status="terminated"),  # filtered out
        ]
    )
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    out = asyncio.run(core._cmd_list("telegram"))
    assert isinstance(out, Reply)
    assert out.text == copy.LIVE_AGENTS_TITLE_BUTTONS
    assert out.buttons is not None
    labels = [b[0] for b in out.buttons]
    cmds = [b[1] for b in out.buttons]
    assert labels == [f"228 {copy.UNNAMED_LABEL} [running]", "405 Ava \u8d1f\u8d23\u4eba [idling]"]
    assert cmds == ["/switch 228", "/switch 405"]


def test_list_on_plain_channel_keeps_full_text_list() -> None:
    """WeChat/Feishu never render buttons — /list keeps the full text list
    there or it would be unusable."""
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    core.register(FakePlainAdapter())
    out = asyncio.run(core._cmd_list("weixin"))
    assert isinstance(out, Reply)
    assert "405  Ava \u8d1f\u8d23\u4eba  [idling]" in out.text
    assert out.buttons is None


def test_chat_typing_starts_and_stops_on_agent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forwarding a chat shows the native typing indicator; the first
    pushed agent reply stops it. The indicator refreshes every few seconds,
    not once."""
    from services.im_bridge import core as core_mod

    monkeypatch.setattr(core_mod, "_TYPING_INTERVAL_S", 0.01)
    monkeypatch.setattr(core_mod, "_TYPING_MAX_S", 60.0)
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405

    async def scenario() -> None:
        out = await core._handle_chat(state, "hi")
        assert out is None
        assert gateway.sent == [(405, "hi", "user")]
        await asyncio.sleep(0.06)  # several refresh ticks
        calls_before = len(adapter.typing_calls)
        assert calls_before >= 3  # refreshed, not a one-shot
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [{"kind": "agent_chat", "item_id": "6.0", "payload": "answer"}]},
        )
        await asyncio.sleep(0.06)
        assert len(adapter.typing_calls) == calls_before  # stopped by the reply
        assert adapter.sent == [("12345", "[Ava #405] answer")]

    asyncio.run(scenario())


def test_typing_skipped_for_plain_adapters() -> None:
    """WeChat/Feishu have no native typing — nothing is sent, and replies
    still land as new messages."""
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakePlainAdapter()
    core.register(adapter)
    state = ChatState("weixin", "67890")
    state.current_agent_id = 405

    async def scenario() -> None:
        await core._handle_chat(state, "hi")
        await asyncio.sleep(0.02)
        assert adapter.typing_calls == []
        await core._push_snapshot(
            ("weixin", "67890"),
            state,
            {"items": [{"kind": "agent_chat", "item_id": "6.0", "payload": "answer"}]},
        )
        assert adapter.sent == [("67890", "[Ava #405] answer")]

    asyncio.run(scenario())


def test_one_typing_loop_per_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second message while the agent still works does not start a second
    typing loop — one indicator per chat."""
    from services.im_bridge import core as core_mod

    monkeypatch.setattr(core_mod, "_TYPING_INTERVAL_S", 0.01)
    gateway = FakeGateway()
    core = _core(gateway)
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
        # still one loop: the rate did not double
        assert len(adapter.typing_calls) <= calls_after_first + 3
        assert gateway.sent == [(405, "first", "user"), (405, "second", "user")]

    asyncio.run(scenario())


def test_typing_gives_up_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent agent does not type forever — the loop ends at the cap."""
    from services.im_bridge import core as core_mod

    monkeypatch.setattr(core_mod, "_TYPING_INTERVAL_S", 0.01)
    monkeypatch.setattr(core_mod, "_TYPING_MAX_S", 0.05)
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405

    async def scenario() -> None:
        await core._handle_chat(state, "hi")
        await asyncio.sleep(0.15)  # well past the cap
        assert core._typing_tasks == {}  # loop finished on its own
        assert len(adapter.typing_calls) >= 1

    asyncio.run(scenario())


# --- v4: /spawn layered menu + /commands ---


def _preset(preset_id: int, name: str, label: str | None = None) -> dict[str, Any]:
    return {
        "id": preset_id,
        "name": name,
        "label": label or name,
        "description": None,
        "config": {},
    }


def _models_data() -> dict[str, Any]:
    return {
        "providers": {"deepseek": ["deepseek-v4-pro", "deepseek-v4-flash"]},
        "models": {
            "deepseek-v4-pro": {
                "provider": "deepseek",
                "context_window": 128000,
                "reasoning_effort_options": ["low", "high", "max"],
            },
            "deepseek-v4-flash": {"provider": "deepseek", "context_window": 128000},
        },
        "default": "deepseek-v4-pro",
    }


def test_spawn_menu_layers_render_with_summary_button() -> None:
    """/spawn walks preset -> model -> effort, each layer carrying the
    summary [Spawn] button; the effort layer offers the model's own options
    plus provider default."""
    gateway = FakeGateway(
        presets=[_preset(1, "coder"), _preset(2, "reviewer", label="Code Reviewer")],
        models=_models_data(),
    )
    core = _core(gateway)
    state = ChatState("telegram", "12345")

    out = asyncio.run(core._cmd_spawn(state))
    assert isinstance(out, Reply)
    assert out.text == copy.SPAWN_LAYER_PRESET
    labels = [b[0] for b in out.buttons or []]
    assert labels[0] == copy.SPAWN_BUTTON_NO_PRESET
    assert "coder" in labels and "Code Reviewer" in labels
    assert labels[-1] == copy.SPAWN_BUTTON_SUMMARY_PREFIX + " / ".join(
        [copy.SPAWN_BUTTON_DEFAULT_VALUE] * 3
    )

    # pick a preset -> model layer
    out2 = asyncio.run(core._handle_spawn_menu(state, "spawn:preset:1"))
    assert isinstance(out2, Reply)
    assert out2.text == copy.SPAWN_LAYER_MODEL
    labels2 = [b[0] for b in out2.buttons or []]
    assert "deepseek-v4-pro" in labels2 and "deepseek-v4-flash" in labels2
    assert labels2[-1] == (
        copy.SPAWN_BUTTON_SUMMARY_PREFIX
        + "coder / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
        + " / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
    )

    # pick a model -> effort layer with the model's own options
    out3 = asyncio.run(core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro"))
    assert isinstance(out3, Reply)
    assert out3.text == copy.SPAWN_LAYER_EFFORT
    labels3 = [b[0] for b in out3.buttons or []]
    assert labels3[0] == copy.SPAWN_BUTTON_PROVIDER_DEFAULT
    assert labels3[1] == "effort: low"
    assert "effort: max" in labels3
    assert labels3[-1] == (
        copy.SPAWN_BUTTON_SUMMARY_PREFIX
        + "coder / deepseek-v4-pro / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
    )


def test_spawn_menu_effort_fallback_without_model_options() -> None:
    """A model without reasoning_effort_options gets the generic set."""
    gateway = FakeGateway(presets=[], models=_models_data())
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_spawn(state))
    asyncio.run(core._handle_spawn_menu(state, "spawn:preset:none"))
    out = asyncio.run(core._handle_spawn_menu(state, "spawn:model:deepseek-v4-flash"))
    assert isinstance(out, Reply)
    labels = [b[0] for b in out.buttons or []]
    assert labels[1] == "effort: low"
    assert labels[-2] == "effort: max"
    assert "effort: medium" in labels


def test_spawn_menu_every_layer_completable() -> None:
    """Spawn directly from layer 1 (no selections) or after picking effort:
    both create the agent with the chosen config."""
    gateway = FakeGateway(presets=[_preset(1, "coder")], models=_models_data())
    core = _core(gateway)
    state = ChatState("telegram", "12345")

    # layer 1, tap Spawn directly -> cluster defaults, no preset
    asyncio.run(core._cmd_spawn(state))
    out = asyncio.run(core._handle_spawn_menu(state, "spawn:go"))
    assert isinstance(out, Reply)
    assert gateway.spawned == [(None, {})]
    assert copy.SPAWNED_PLAIN.format(agent_id=777) in out.text  # no preset -> generic label
    assert out.buttons == [(copy.SPAWN_SWITCH_BUTTON.format(agent_id=777), "/switch 777")]

    # full path: preset -> model -> effort -> go
    asyncio.run(core._cmd_spawn(state))
    asyncio.run(core._handle_spawn_menu(state, "spawn:preset:1"))
    asyncio.run(core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro"))
    asyncio.run(core._handle_spawn_menu(state, "spawn:effort:max"))
    out2 = asyncio.run(core._handle_spawn_menu(state, "spawn:go"))
    assert isinstance(out2, Reply)
    assert gateway.spawned[-1] == (
        "coder",
        {"llm_model": "deepseek-v4-pro", "reasoning_effort": "max"},
    )
    assert copy.SPAWNED_WITH_PRESET.format(preset="coder", agent_id=777) in out2.text


def test_spawn_menu_provider_default_effort_means_unset() -> None:
    gateway = FakeGateway(presets=[_preset(1, "coder")], models=_models_data())
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_spawn(state))
    asyncio.run(core._handle_spawn_menu(state, "spawn:preset:1"))
    asyncio.run(core._handle_spawn_menu(state, "spawn:model:deepseek-v4-pro"))
    asyncio.run(core._handle_spawn_menu(state, "spawn:effort:"))  # provider default
    asyncio.run(core._handle_spawn_menu(state, "spawn:go"))
    assert gateway.spawned[-1] == ("coder", {"llm_model": "deepseek-v4-pro"})


def test_spawn_skips_preset_layer_when_no_presets() -> None:
    """No presets configured — /spawn jumps straight to the model layer."""
    gateway = FakeGateway(presets=[], models=_models_data())
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_spawn(state))
    assert isinstance(out, Reply)
    assert out.text == copy.SPAWN_LAYER_MODEL


def test_spawn_menu_stale_preset_reports_error() -> None:
    gateway = FakeGateway(presets=[_preset(1, "coder")], models=_models_data())
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    asyncio.run(core._cmd_spawn(state))
    out = asyncio.run(core._handle_spawn_menu(state, "spawn:preset:99"))
    assert isinstance(out, Reply)
    assert copy.SPAWN_PRESET_GONE in out.text


def test_commands_lists_everything() -> None:
    core = _core(FakeGateway())
    out = core._cmd_help()  # sync — no gateway call
    text = _text(out)
    assert "/list" in text and "/spawn" in text
    assert "/status" in text and "/commands" in text
    assert "/switch" not in text  # /list buttons switch now (user ruling 2026-08-04)


def test_commands_lists_ava_slash_catalog_as_buttons() -> None:
    """/commands is the Ava slash-command catalog: on button channels each
    command is a tap target, descriptions are truncated, and no em-dash
    reaches the user (user ruling 2026-08-04)."""
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
    core = _core(gateway)
    core.register(FakeTypingAdapter())  # can_buttons = True
    out = asyncio.run(core._cmd_commands("telegram"))
    assert isinstance(out, Reply)
    assert "/audio-transcribe" in out.text
    assert "/ava-fleet" in out.text
    assert copy.COMMANDS_HEADER in out.text
    assert copy.COMMANDS_INTRO.format(count=2) in out.text
    assert "—" not in out.text  # the em-dash does not render on Telegram
    # description truncated to 60 chars with an ASCII ellipsis
    assert "…" not in out.text
    assert "media URLs" not in out.text  # cut before the tail of the description
    assert "or me..." in out.text
    # every command is a button; tapping sends /name to the current agent
    assert out.buttons is not None
    assert ("/audio-transcribe", "/audio-transcribe") in out.buttons
    assert ("/ava-fleet", "/ava-fleet") in out.buttons


def test_commands_plain_channel_keeps_text_only() -> None:
    """WeChat/Feishu render no buttons — /commands stays a text list."""
    gateway = FakeGateway()
    core = _core(gateway)
    core.register(FakePlainAdapter())
    out = asyncio.run(core._cmd_commands("weixin"))
    assert isinstance(out, Reply)
    assert "/audio-transcribe" in out.text
    assert out.buttons is None


def test_commands_empty_catalog() -> None:
    gateway = FakeGateway()
    gateway.commands = []
    core = _core(gateway)
    out = asyncio.run(core._cmd_commands("telegram"))
    assert _text(out) == copy.NO_COMMANDS_REGISTERED


def test_unknown_slash_command_forwards_to_current_agent() -> None:
    """ "/audio-transcribe <src>" on IM must reach the current agent — its
    claim node expands registered commands like the web composer does."""
    gateway = FakeGateway()
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    out = asyncio.run(core._handle_command(state, "/audio-transcribe go"))
    assert out is None  # forwarded, no IM-level reply
    assert gateway.sent == [(405, "/audio-transcribe go", "user")]


def test_unknown_slash_command_without_agent_errors() -> None:
    gateway = FakeGateway()
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._handle_command(state, "/audio-transcribe go"))
    assert out is not None
    assert copy.NO_AGENT_SWITCHED in _text(out)
    assert gateway.sent == []


# --- Task #829: weixin push-failure alerting + recovered hint ---


class FakeFailingWeixinAdapter(FakePlainAdapter):
    """Weixin adapter stand-in whose sends always fail, with the watchdog
    state the real adapter maintains (push_failures / push_failed_at /
    push_recovered_at / _push_alerted_at)."""

    def __init__(self) -> None:
        super().__init__()
        self.channel = "weixin"
        self.push_failures = 0
        self.push_failed_at: float | None = None
        self.push_recovered_at: float | None = None
        self._push_alerted_at: float | None = None
        self.owner_alerts: list[str] = []

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        raise RuntimeError("iLink sendmessage error: ret=-2 errmsg=prepare failed")

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        del markdown
        self.owner_alerts.append(text)


def test_weixin_push_failures_alert_other_channel() -> None:
    """After enough consecutive weixin send failures, the user is alerted
    through another channel (Telegram) — inbound-only failure is invisible
    to the user otherwise (Task #829)."""
    gateway = FakeGateway()
    core = _core(gateway)
    wx = FakeFailingWeixinAdapter()
    tg = FakeTypingAdapter()
    core.register(wx)
    core.register(tg)
    wx.push_failures = 2
    wx.push_failed_at = 1234.0

    async def scenario() -> None:
        await core._send("weixin", "o9cq804", Reply("hello"))
        # send + retry both fail; the alert goes out on telegram's owner chat
        assert any("push link failed" in t for t in tg.owner_sent)
        assert wx._push_alerted_at is not None

    asyncio.run(scenario())


def test_weixin_push_failure_alert_cooldown() -> None:
    """The cross-channel alert fires at most once per cooldown window."""
    gateway = FakeGateway()
    core = _core(gateway)
    wx = FakeFailingWeixinAdapter()
    tg = FakeTypingAdapter()
    core.register(wx)
    core.register(tg)
    wx.push_failures = 2
    wx.push_failed_at = 100.0
    wx._push_alerted_at = 100.0  # just alerted

    async def scenario() -> None:
        # same window -> no second alert
        await core._send("weixin", "o9cq804", Reply("hello"))
        assert wx.owner_alerts == []

    asyncio.run(scenario())


def test_weixin_push_recovery_hints_on_next_inbound() -> None:
    """When a user message brings a fresh token and the first send succeeds,
    the next inbound reply carries a 'recovered' hint (Task #829)."""
    gateway = FakeGateway()
    core = _core(gateway)
    wx = FakeFailingWeixinAdapter()
    core.register(wx)
    # simulate: push had failed, then a user message refreshed the token and
    # a send succeeded just now
    import time as _time

    wx.push_failures = 0
    wx.push_recovered_at = _time.time()  # just recovered
    wx.push_failed_at = _time.time() - 5

    # make _send succeed on weixin (override the failing adapter)
    async def ok_send(
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        wx.sent.append((chat_id, text))

    wx.send = ok_send  # type: ignore[method-assign]
    from services.im_bridge.types import InboundMessage

    async def scenario() -> None:
        await core.handle_inbound(
            InboundMessage(channel="weixin", chat_id="o9cq804", text="\u5728", message_id="m1")
        )
        texts = [t for _, t in wx.sent]
        assert any("push link failed earlier and has now recovered" in t for t in texts)

    asyncio.run(scenario())


def test_restore_subscriptions_skips_disabled_channels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Disabled channels (AVA_IM_DISABLED_ADAPTERS) get no restored
    subscription — stale switch_state bindings are skipped so the bridge
    stops pushing snapshots to a channel with no adapter (Task #855)."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    monkeypatch.setattr(settings.services, "im_disabled_adapters", [])
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)  # nothing disabled: both subscribe
    for channel, chat_id in (("weixin", "wx123"), ("telegram", "12345")):
        state = ChatState(channel, chat_id)
        asyncio.run(core._cmd_switch(state, "405"))
        assert (channel, chat_id) in core._subscriptions

    # daemon restart with weixin disabled: its subscription is not restored
    monkeypatch.setattr(settings.services, "im_disabled_adapters", ["weixin"])
    core2 = _core(FakeGateway(agents=[]))
    assert core2._disabled_channels == {"weixin"}
    asyncio.run(core2.restore_subscriptions())
    assert ("weixin", "wx123") not in core2._subscriptions
    assert ("telegram", "12345") in core2._subscriptions


# --- Task #1032: three P0s — watermark compare / inbound outbox / SSE log ---


def test_push_snapshot_watermark_compares_numerically() -> None:
    """Regression #1032: the watermark filter must compare item_ids
    numerically. The old string compare treated '9.5' > '10.1' as false, so
    the first message past the 10-message boundary silently stopped all
    pushes; the reverse direction would also re-push stale items."""
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405

    def snapshot(item_id: str, payload: str) -> dict[str, Any]:
        return {"items": [{"item_id": item_id, "kind": "agent_chat", "payload": payload}]}

    async def scenario() -> None:
        core._last_pushed[("telegram", "12345", 405)] = "9.5"
        # crossing the magnitude boundary: '10.1' is fresh after '9.5'
        await core._push_snapshot(("telegram", "12345"), state, snapshot("10.1", "ten"))
        assert adapter.sent == [("12345", "[Ava #405] ten")]
        assert core._last_pushed[("telegram", "12345", 405)] == "10.1"
        # the reverse: an older item behind a newer watermark is stale
        await core._push_snapshot(("telegram", "12345"), state, snapshot("9.9", "nine"))
        assert adapter.sent == [("12345", "[Ava #405] ten")]  # unchanged

    asyncio.run(scenario())


def test_sse_reconnect_log_does_not_kill_subscription_loop() -> None:
    """Regression #1032: the reconnect log call used a loguru '{}' placeholder
    on the stdlib logger; logging raised TypeError ('not all arguments
    converted') inside the except block, the surrounding try does not catch
    except-block exceptions, and the subscription loop — and with it all
    pushes — died. The loop must survive a stream error and its log call."""
    gateway = FakeGateway(stream_failures=1)
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405

    async def scenario() -> None:
        task = asyncio.create_task(core._subscription_loop(("telegram", "12345"), state))
        await asyncio.sleep(0.3)  # stream error → reconnect log → 5s sleep
        assert not task.done(), "the reconnect log call killed the loop"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_handle_inbound_outboxes_when_gateway_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Regression #1032: when the gateway enqueue fails after every retry the
    user message used to be silently dropped (AtLeastOnce broken). It must be
    persisted to the outbox and the user told it is queued."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway(send_failures=10)
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = core._get_or_create_state("telegram", "12345")
    state.current_agent_id = 405
    msg = InboundMessage(channel="telegram", chat_id="12345", text="hello")

    async def scenario() -> None:
        await core.handle_inbound(msg)
        assert gateway.sent == []  # gateway is down — nothing delivered
        outbox = state_mod._load_outbox()
        assert len(outbox) == 1
        assert outbox[0].text == "hello"
        assert outbox[0].agent_id == 405
        assert any("messages queued" in t for _, t in adapter.sent)

    asyncio.run(scenario())


def test_outbox_replay_delivers_and_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Regression #1032: after the gateway recovers, the replay drains the
    outboxed message with its persisted Idempotency-Key (AtLeastOnce) and
    clears the file. The key must be replayed unchanged so the gateway dedups
    a lost-response retry server-side."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    gateway = FakeGateway(send_failures=1)
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    core.register(adapter)
    state = core._get_or_create_state("telegram", "12345")
    state.current_agent_id = 405
    msg = InboundMessage(channel="telegram", chat_id="12345", text="hello")

    async def scenario() -> None:
        await core.handle_inbound(msg)  # first send fails → outboxed
        entry = state_mod._load_outbox()[0]
        await core._replay_outbox_once()  # gateway back — drains
        assert gateway.sent == [(405, "hello", "user")]
        assert gateway.sent_keys == [entry.idempotency_key]
        assert state_mod._load_outbox() == []

    asyncio.run(scenario())


def test_push_snapshot_watermark_is_per_chat() -> None:
    """Regression (audit round 2, P1): the watermark used to be keyed by
    agent alone, so two chats switched to the same agent clobbered each
    other — the second chat's snapshot advanced the shared watermark past
    what the first chat had pushed, and the first chat permanently missed
    those items. The key must be (channel, chat_id, agent_id)."""
    gateway = FakeGateway()
    core = _core(gateway)
    adapter = FakeTypingAdapter()
    plain = FakePlainAdapter()
    core.register(adapter)
    core.register(plain)
    state_a = ChatState("telegram", "12345")
    state_a.current_agent_id = 405
    state_b = ChatState("weixin", "wx123")
    state_b.current_agent_id = 405

    def snapshot(*item_ids: str) -> dict[str, Any]:
        return {
            "items": [{"item_id": i, "kind": "agent_chat", "payload": f"p{i}"} for i in item_ids]
        }

    async def scenario() -> None:
        # Both chats receive the same snapshot (same agent); each must push
        # every item — a shared watermark would make the second chat stale.
        await core._push_snapshot(("telegram", "12345"), state_a, snapshot("1.1", "2.1"))
        await core._push_snapshot(("weixin", "wx123"), state_b, snapshot("1.1", "2.1"))
        assert adapter.sent == [
            ("12345", "[Ava #405] p1.1"),
            ("12345", "[Ava #405] p2.1"),
        ]
        assert plain.sent == [
            ("wx123", "[Ava #405] p1.1"),
            ("wx123", "[Ava #405] p2.1"),
        ]
        # A new item for chat A alone must not be suppressed by chat B's
        # watermark (and vice versa).
        await core._push_snapshot(("telegram", "12345"), state_a, snapshot("3.1"))
        await core._push_snapshot(("weixin", "wx123"), state_b, snapshot("4.1"))
        assert adapter.sent[-1:] == [("12345", "[Ava #405] p3.1")]
        assert plain.sent[-1:] == [("wx123", "[Ava #405] p4.1")]
        assert core._last_pushed[("telegram", "12345", 405)] == "3.1"
        assert core._last_pushed[("weixin", "wx123", 405)] == "4.1"

    asyncio.run(scenario())
