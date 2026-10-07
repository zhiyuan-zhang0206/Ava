"""`services.entrypoints.im_bridge.core` command routing against the real gateway shape.

Regression guard: GET /api/agents rows carry ``agent_id`` (not ``id``) — the
first field-name mismatch made ``/list`` crash with KeyError('id') in prod
(2026-08-03). Every fake below uses the real gateway row shape, so a revert
to ``a["id"]`` fails immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.slices import im_bridge_config
from services.entrypoints.im_bridge.types import ChatState, IMAdapter, Reply, SendNotStartedError


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


TEST_POOL: Any = None


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
        self.timeline = [
            dict(item, source_message_id=f"stored-{index}", source_block_idx=0)
            for index, item in enumerate(timeline or [])
        ]
        self.presets = presets or []
        self.models = models or {"models": {}, "default": "deepseek-v4-pro"}
        self.commands: list[dict[str, Any]] | None = None
        self.sent: list[tuple[int, str, str]] = []
        self.sent_keys: list[str | None] = []
        self.spawned: list[tuple[str | None, dict[str, object] | None]] = []
        self.creation_keys: list[str | None] = []
        self.timeline_limits: list[int | None] = []
        self.send_failures = send_failures
        self.stream_failures = stream_failures
        self.directory_calls: list[tuple[str, str, int | None]] = []
        self.detail_calls: list[int] = []

    async def list_agents(
        self, *, scope: str, query: str = "", before_id: int | None = None
    ) -> dict[str, Any]:
        self.directory_calls.append((scope, query, before_id))
        matches = sorted(
            (
                agent
                for agent in self.agents
                if (scope == "all" or (agent["status"] == "terminated") == (scope == "terminated"))
                and query.casefold() in (agent["label"] or "").casefold()
                and (before_id is None or agent["agent_id"] < before_id)
            ),
            key=lambda agent: agent["agent_id"],
            reverse=True,
        )
        page = matches[:100]
        return {
            "agents": [
                {key: agent[key] for key in ("agent_id", "label", "status")} for agent in page
            ],
            "next_cursor": page[-1]["agent_id"] if len(matches) > 100 else None,
        }

    async def get_agent(self, agent_id: int) -> dict[str, Any] | None:
        self.detail_calls.append(agent_id)
        return next((agent for agent in self.agents if agent["agent_id"] == agent_id), None)

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

    async def spawn_agent(
        self,
        *,
        preset: str | None,
        config: dict[str, object] | None,
        idempotency_key: str | None = None,
    ) -> int:
        self.spawned.append((preset, config))
        self.creation_keys.append(idempotency_key)
        return 777

    async def get_timeline(self, agent_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        self.timeline_limits.append(limit)
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


def create_test_core(gateway: FakeGateway, **config: Any) -> IMBridgeCore:
    core = IMBridgeCore(im_bridge_config(**config), gateway, db_pool=TEST_POOL)  # type: ignore[arg-type]
    telegram = FakePlainAdapter()
    telegram.channel = "telegram"
    core.register(telegram)
    core.register(FakePlainAdapter())
    return core


_core = create_test_core


def _queued_text(core: IMBridgeCore) -> str:
    with core.timeline_outbox._pool().connection() as conn:
        rows = conn.execute("SELECT request FROM im_bridge_outbound_intents ORDER BY id").fetchall()
    return "\n".join(
        chunk["text"] for (request,) in rows for chunk in request["prepared"]["chunks"]
    )


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


def test_cmd_list_pages_live_agents_only() -> None:
    gateway = FakeGateway(
        agents=[_row(agent_id) for agent_id in range(1, 102)]
        + [_row(agent_id, status="terminated") for agent_id in range(102, 120)]
    )
    text = _text(asyncio.run(_core(gateway)._cmd_list("telegram")))
    assert len(text.splitlines()) == 102
    assert gateway.directory_calls == [("live", "", None), ("live", "", 2)]
    assert gateway.detail_calls == []


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
    assert "hello" not in _text(out), (
        "dialog is queued instead of returned through the command send owner"
    )
    text = _text(out) + "\n" + _queued_text(core)
    assert state.current_agent_id == 405
    assert copy.SWITCHED_TO.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in text
    assert "hello" in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == PushWatermark(None, "3.1")
    assert gateway.directory_calls == []
    assert gateway.detail_calls == [405]


def test_cmd_switch_matches_label() -> None:
    gateway = FakeGateway(agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")])
    core = _core(gateway)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "ava \u8d1f\u8d23\u4eba"))
    assert state.current_agent_id == 405
    assert copy.SWITCHED_TO.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in _text(out)
    assert gateway.directory_calls == [("live", "ava \u8d1f\u8d23\u4eba", None)]


def test_cmd_switch_label_pages_exact_matches_and_prefers_live() -> None:
    gateway = FakeGateway(
        agents=[_row(1, label="Target")]
        + [_row(agent_id, label="target-extra") for agent_id in range(2, 102)]
        + [_row(102, label="target", status="terminated")]
    )
    state = ChatState("telegram", "12345")
    asyncio.run(_core(gateway)._cmd_switch(state, "TARGET"))
    assert state.current_agent_id == 1
    assert gateway.directory_calls == [("live", "TARGET", None), ("live", "TARGET", 2)]
    assert gateway.detail_calls == []


def test_cmd_switch_label_rejects_partial_and_terminated_matches() -> None:
    gateway = FakeGateway(
        agents=[_row(1, label="target-extra"), _row(2, label="target", status="terminated")]
    )
    state = ChatState("telegram", "12345")
    reply = asyncio.run(_core(gateway)._cmd_switch(state, "target"))
    assert state.current_agent_id is None
    assert _text(reply) == copy.AGENT_CANNOT_SWITCH.format(agent_id=2, status="terminated")
    assert gateway.directory_calls == [("live", "target", None), ("terminated", "target", None)]


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
    text = _text(out) + "\n" + _queued_text(core)
    for m in ("m1", "m2", "m3", "m4", "m5"):
        assert m in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == PushWatermark(None, "5.1")


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
    text = _text(out) + "\n" + _queued_text(core)
    for m in ("m1", "m2", "m3"):
        assert m not in text
    for m in ("m4", "m5", "m6", "m7", "m8"):
        assert m in text
    assert core._last_pushed.get(("telegram", "12345", 405)) == PushWatermark(None, "8.1")


def test_cmd_switch_window_and_replay_follow_config() -> None:
    """The raw fetch window and the replay count are cluster config (task #3696)."""
    gateway = FakeGateway(
        agents=[_row(405, label="Ava \u8d1f\u8d23\u4eba")],
        timeline=[
            {"kind": "agent_chat", "item_id": f"{i}.1", "payload": f"m{i}"} for i in range(1, 9)
        ],
    )
    core = _core(gateway, im_bridge_timeline_window=7, im_bridge_replay_messages=2)
    state = ChatState("telegram", "12345")
    out = asyncio.run(core._cmd_switch(state, "405"))
    text = _text(out) + "\n" + _queued_text(core)
    assert gateway.timeline_limits[-1] == 7
    for m in ("m1", "m2", "m3", "m4", "m5", "m6"):
        assert m not in text
    for m in ("m7", "m8"):
        assert m in text


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
    text = _text(out) + "\n" + _queued_text(core)
    assert copy.STATUS_DETAIL_LINE.format(agent_id=405, label="Ava \u8d1f\u8d23\u4eba") in text
    assert copy.STATUS_STATE_LINE.format(status="running") in text
    assert gateway.directory_calls == []
    assert gateway.detail_calls == [405]


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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
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
    from services.entrypoints.im_bridge.core import _is_dialog_item

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
    text = _text(out) + "\n" + _queued_text(core)
    assert "real answer" in text
    assert "peer" not in text
    assert "print(1)" not in text
    # Only the command header returns inline; the qualified dialog is durably queued.
    assert isinstance(out, list) and len(out) == 1
    assert _queued_text(core) == "[Ava #405] real answer"


def test_render_item_tags_speaker() -> None:
    """Pushed lines carry the [User] / [Ava #<id>] speaker tag."""
    from services.entrypoints.im_bridge.core import _render_item

    assert _render_item({"kind": "inbound_chat", "payload": "hi"}, 405) == "[User] hi"
    assert _render_item({"kind": "agent_chat", "payload": "answer"}, 405) == "[Ava #405] answer"


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


def _assert_preset_layer(out: object) -> None:
    assert isinstance(out, Reply)
    assert out.text == copy.SPAWN_LAYER_PRESET
    labels = [b[0] for b in out.buttons or []]
    assert labels[0] == copy.SPAWN_BUTTON_NO_PRESET
    assert "coder" in labels and "Code Reviewer" in labels
    assert labels[-1] == copy.SPAWN_BUTTON_SUMMARY_PREFIX + " / ".join(
        [copy.SPAWN_BUTTON_DEFAULT_VALUE] * 3
    )


def _assert_model_layer_after_coder(out: object) -> None:
    assert isinstance(out, Reply)
    assert out.text == copy.SPAWN_LAYER_MODEL
    labels = [b[0] for b in out.buttons or []]
    assert "deepseek-v4-pro" in labels and "deepseek-v4-flash" in labels
    assert labels[-1] == (
        copy.SPAWN_BUTTON_SUMMARY_PREFIX
        + "coder / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
        + " / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
    )


def _assert_effort_layer_after_pro(out: object) -> None:
    assert isinstance(out, Reply)
    assert out.text == copy.SPAWN_LAYER_EFFORT
    labels = [b[0] for b in out.buttons or []]
    assert labels[0] == copy.SPAWN_BUTTON_PROVIDER_DEFAULT
    assert labels[1] == "effort: low"
    assert "effort: max" in labels
    assert labels[-1] == (
        copy.SPAWN_BUTTON_SUMMARY_PREFIX
        + "coder / deepseek-v4-pro / "
        + copy.SPAWN_BUTTON_DEFAULT_VALUE
    )


# --- Task #829: weixin push-failure event + recovered hint ---


class FakeFailingWeixinAdapter(FakePlainAdapter):
    """Weixin adapter stand-in whose sends always fail, with the watchdog
    state the real adapter maintains (push_failures / push_failed_at /
    push_recovered_at)."""

    def __init__(self) -> None:
        super().__init__()
        self.channel = "weixin"
        self.push_failures = 0
        self.push_failed_at: float | None = None
        self.push_recovered_at: float | None = None
        self.send_attempts = 0

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        self.send_attempts += 1
        raise RuntimeError("iLink sendmessage error: ret=-2 errmsg=prepare failed")


class FakeFlakyWeixinAdapter(FakePlainAdapter):
    """Weixin adapter stand-in whose first send fails and whose retry
    succeeds — the probe's self-heal case (task #4252). Every attempt is
    appended to `events`, so a test can assert the backoff interleaving."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.channel = "weixin"
        self.events = events
        self.push_failures = 0
        self.push_failed_at: float | None = None
        self.push_recovered_at: float | None = None
        self.send_attempts = 0

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        self.send_attempts += 1
        self.events.append("send")
        if self.send_attempts == 1:
            self.push_failures += 1
            self.push_failed_at = time.time()
            raise SendNotStartedError("connection failed before any send")
        if self.push_failures > 0:
            self.push_recovered_at = time.time()  # the real adapter's reset path
        self.push_failures = 0
        self.sent.append((chat_id, text))


# --- Task #4252: push retry — bounded jitter backoff, exactly one retry ---


# --- Task #1032: three P0s — watermark compare / inbound outbox / SSE log ---


async def test_spawn_submission_forwards_adapter_event_key() -> None:
    from services.entrypoints.im_bridge.types import InboundMessage

    gateway = FakeGateway()
    core = _core(gateway)
    await core.handle_inbound(
        InboundMessage(
            channel="telegram",
            chat_id="42",
            text="spawn:go",
            idempotency_key="telegram-callback:one",
        )
    )
    assert gateway.creation_keys == ["telegram-callback:one"]
