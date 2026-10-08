"""Unit tests for services/entrypoints/im_bridge/adapters/weixin.py.

Covers the contract surface: qualified getUpdates messages commit native admission
(and their context_token is cached to disk), sendmessage echoes the cached
context_token with the iLink headers, long texts segment at 2000 chars, stale
sessions retry once without the token, HTTP failures surface as sanitized
RuntimeErrors, an unconfigured adapter skips start() without crashing, and the
QR login flow persists credentials (chmod 600).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest

from services.entrypoints.im_bridge.adapters import weixin
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter
from services.entrypoints.im_bridge.adapters.weixin_login import qr_login
from services.entrypoints.im_bridge.ingress.tests.conftest import NativeWeixin
from services.entrypoints.im_bridge.ingress.tests.conftest import native_weixin as native_weixin
from services.entrypoints.im_bridge.ingress.types import IngressReceipt, IngressStatus
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from services.entrypoints.im_bridge.types import InboundMessage


class FakeCore:
    """Records inbound messages; signals when one arrives."""

    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []
        self.received = asyncio.Event()

    async def handle_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)
        self.received.set()


def _message(
    *,
    text: str,
    from_user_id: str = "peer-1",
    message_id: str = "1",
    context_token: str | None = "tok-ctx-1",  # noqa: S107  (test fixture value)
    message_type: int = 1,
    room_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from_user_id": from_user_id,
        "to_user_id": "bot-id",
        "message_id": message_id,
        "message_type": message_type,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token is not None:
        message["context_token"] = context_token
    if room_id is not None:
        message["room_id"] = room_id
    return message


def _updates(*messages: dict[str, Any], sync_buf: str = "buf-1") -> dict[str, Any]:
    return {"ret": 0, "msgs": list(messages), "get_updates_buf": sync_buf}


def _transport(
    script: list[httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """MockTransport that records requests and replays ``script`` responses."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if script:
            return script.pop(0)
        return httpx.Response(200, json=_updates())

    return httpx.MockTransport(handler), captured


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Path:
    """Point the state dir at a tmp AVA_HOME and write a test account."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    account = {
        "account_id": "bot-id",
        "bot_token": "test-bot-token",
        "user_id": "bot-user-id",
        "base_url": "https://ilinkai.weixin.qq.com",
    }
    path = tmp_path / "state" / "im_bridge" / "weixin_account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(account))
    return tmp_path


def _state_file(tmp_path: Any, name: str) -> Path:
    return tmp_path / "state" / "im_bridge" / name


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not Windows ACLs")
def test_atomic_json_preserves_bytes_and_tightens_private_modes(tmp_path: Path) -> None:
    path = tmp_path / "state" / "im_bridge" / "weixin_sync.json"
    path.parent.mkdir(parents=True, mode=0o755)
    path.parent.chmod(0o755)
    payload = {"token": "café", "items": [1, 2]}

    weixin._atomic_write_json(path, payload)

    assert path.read_bytes() == json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(path.parent.iterdir()) == [path]


def test_atomic_json_concurrent_writes_use_distinct_temps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "im_bridge" / "weixin_sync.json"
    path.parent.mkdir(parents=True)
    barrier = Barrier(2)
    sources: list[Path] = []
    original_replace = os.replace

    def rendezvous(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        sources.append(Path(source))
        barrier.wait(timeout=5)
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", rendezvous)
    values = [{"token": "first" * 4096}, {"token": "second" * 4096}]

    def write(value: dict[str, str]) -> None:
        weixin._atomic_write_json(path, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, values))

    assert json.loads(path.read_bytes()) in values
    assert len(set(sources)) == 2
    assert all(source.parent == path.parent for source in sources)
    assert all(
        source.name.startswith(f".{path.name}.") and source.suffix == ".tmp" for source in sources
    )
    assert sorted(path.parent.iterdir()) == [path]


def test_atomic_json_failed_replace_keeps_old_content_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "im_bridge" / "weixin_sync.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")

    def fail_replace(_source: os.PathLike[str] | str, _target: os.PathLike[str] | str) -> None:
        raise OSError("replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        weixin._atomic_write_json(path, {"new": True})
    assert path.read_bytes() == b"old"
    assert sorted(path.parent.iterdir()) == [path]


async def test_poll_forwards_message_and_caches_context_token(native_weixin: NativeWeixin) -> None:
    adapter = native_weixin.adapter
    native_weixin.provider_responses.append(
        _updates(_message(text="hello", context_token="tok-abc"))  # noqa: S106
    )
    cursor, _ = await adapter._poll_once("", 35000)
    assert cursor == "buf-1"
    assert adapter._tokens.get("peer-1") == "tok-abc"
    assert json.loads(adapter._tokens._path.read_text()) == {"peer-1": "tok-abc"}
    assert not (adapter._tokens._path.parent / "weixin_sync.json").exists()


async def test_send_echoes_context_token_and_headers(env: Any) -> None:
    """sendmessage carries the peer's cached context_token and the iLink headers."""
    transport, captured = _transport([httpx.Response(200, json={"ret": 0})])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "tok-ctx-9")
        await adapter.send("peer-1", "hi")

    (request,) = captured
    msg = json.loads(request.content)["msg"]
    assert msg["to_user_id"] == "peer-1"
    assert msg["context_token"] == "tok-ctx-9"  # noqa: S105  (test fixture value)
    assert msg["item_list"][0]["text_item"]["text"] == "hi"
    assert request.url.path == "/ilink/bot/sendmessage"
    assert request.headers["Authorization"] == "Bearer test-bot-token"
    assert request.headers["AuthorizationType"] == "ilink_bot_token"
    assert request.headers["iLink-App-Id"] == "bot"
    assert request.headers["iLink-App-ClientVersion"]
    assert request.headers["X-WECHAT-UIN"]


async def test_send_splits_long_text(env: Any) -> None:
    """Text longer than 2000 chars arrives in consecutive 2000-char chunks."""
    transport, captured = _transport([httpx.Response(200, json={"ret": 0}) for _ in range(3)])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._chunk_delay_seconds = 0
        await adapter.send("peer-1", "x" * 4500)

    bodies = [json.loads(r.content)["msg"] for r in captured]
    texts = [b["item_list"][0]["text_item"]["text"] for b in bodies]
    assert texts == ["x" * 2000, "x" * 2000, "x" * 500]
    assert all(b["to_user_id"] == "peer-1" for b in bodies)


async def test_session_expired_retries_without_token(env: Any) -> None:
    """errcode -14 -> one retry without the token, then success."""
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ret": -14, "errmsg": "session expired"}),
            httpx.Response(200, json={"ret": 0}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "stale-tok")
        await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert bodies[0]["context_token"] == "stale-tok"  # noqa: S105  (test fixture value)
    assert "context_token" not in bodies[1]
    assert adapter._tokens.get("peer-1") is None


@pytest.mark.parametrize(
    "errmsg",
    ["prepare failed", "unknown error", "", None],
)
async def test_stale_errmsg_retries_without_token(env: Any, errmsg: str | None) -> None:
    """ret=-2 with a stale-session errmsg (or none) -> one tokenless retry.

    iLink reports an expired context_token ambiguously: "prepare failed",
    "unknown error", or an empty errmsg. All three must trigger the same
    degraded retry path as errcode -14, otherwise outbound pushes after a
    long idle fail forever.
    """
    transport, captured = _transport(
        [
            httpx.Response(200, json={"ret": -2, "errmsg": errmsg}),
            httpx.Response(200, json={"ret": 0}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "stale-tok")
        await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert bodies[0]["context_token"] == "stale-tok"  # noqa: S105  (test fixture value)
    assert "context_token" not in bodies[1]
    assert adapter._tokens.get("peer-1") is None


async def test_rate_limit_errmsg_not_stale(env: Any) -> None:
    """ret=-2 with a populated rate-limit errmsg is NOT a stale session.

    A genuine rate limit must keep raising so the caller sees the failure
    instead of burning the token on a pointless retry.
    """
    transport, captured = _transport(
        [httpx.Response(200, json={"ret": -2, "errmsg": "frequency limit"})]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._tokens.set("peer-1", "tok")
        with pytest.raises(RuntimeError, match="frequency limit"):
            await adapter.send("peer-1", "hi")

    bodies = [json.loads(r.content)["msg"] for r in captured]
    assert len(bodies) == 1  # no tokenless retry
    assert bodies[0]["context_token"] == "tok"  # noqa: S105  (test fixture value)


async def test_send_sanitizes_http_error(env: Any) -> None:
    """A non-200 send raises a RuntimeError without the token or the URL."""
    transport, _captured = _transport([httpx.Response(500, text="boom")])
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="HTTP 500") as exc_info:
            await adapter.send("peer-1", "hello")
    assert "test-bot-token" not in str(exc_info.value)
    assert "ilinkai.weixin.qq.com" not in str(exc_info.value)


async def test_transport_error_sanitized(env: Any) -> None:
    """httpx transport errors surface as the exception type, never the URL."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://ilinkai.weixin.qq.com/ilink/bot/sendmessage")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        with pytest.raises(RuntimeError, match="ConnectError") as exc_info:
            await adapter.send("peer-1", "hello")
    assert "ilinkai.weixin.qq.com" not in str(exc_info.value)


async def test_skips_echo_group_bot_and_textless(native_weixin: NativeWeixin) -> None:
    adapter = native_weixin.adapter
    payloads = [
        _message(text="echo", from_user_id="bot-id", message_id="1"),
        _message(text="group", room_id="room-1", message_id="2"),
        _message(text="bot", message_type=2, message_id="3"),
        _message(text="", message_id="4"),
    ]
    payloads[-1]["item_list"] = [{"type": 2, "image_item": {"media": {"url": "x"}}}]
    receipts = [await adapter._handle_message(payload) for payload in payloads]
    assert all(receipt.status == IngressStatus.REJECTED for receipt in receipts)
    assert all(receipt.inbound_id is None for receipt in receipts)


async def test_unconfigured_start_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No account file -> start() does nothing and send() raises."""
    async with owned_tasks() as _owned_tasks:
        monkeypatch.setenv("AVA_HOME", str(tmp_path))
        adapter = WeixinAdapter(FakeCore())
        assert not adapter._configured
        await adapter.start(_owned_tasks)
        assert adapter._poll_task is None
        with pytest.raises(RuntimeError, match="not configured"):
            await adapter.send("peer-1", "hi")


async def test_qr_login_saves_account(env: Any, tmp_path: Any) -> None:
    """QR flow: fetch QR, poll status until confirmed, persist credentials."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "get_bot_qrcode" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "qrcode": "hex-token",
                    "qrcode_img_content": "https://wx.qq.com/scan-me",
                },
            )
        if "get_qrcode_status" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "ilink_bot_id": "bot-42",
                    "bot_token": "tok-42",
                    "ilink_user_id": "user-42",
                    "baseurl": "https://ilinkai.weixin.qq.com",
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        creds = await qr_login(client=client, timeout_seconds=30)

    assert creds == {
        "account_id": "bot-42",
        "bot_token": "tok-42",
        "base_url": "https://ilinkai.weixin.qq.com",
        "user_id": "user-42",
    }
    account = _state_file(tmp_path, "weixin_account.json")
    saved = json.loads(account.read_text())
    assert saved["bot_token"] == "tok-42"  # noqa: S105  (test fixture value)
    assert saved["account_id"] == "bot-42"
    assert (account.stat().st_mode & 0o777) == 0o600


async def test_message_from_owner_is_delivered(native_weixin: NativeWeixin) -> None:
    core = native_weixin.core
    state = core._get_or_create_state("weixin", "owner")
    await core._cmd_switch(state, str(native_weixin.agent_id), replay_id="owner-selection")
    receipt = await native_weixin.adapter._handle_message(_message(text="hi", from_user_id="owner"))
    assert receipt.status == IngressStatus.ACCEPTED
    assert receipt.source.sender_id == "owner"
    assert receipt.route.agent_id == native_weixin.agent_id


async def test_message_from_bot_itself_is_dropped(native_weixin: NativeWeixin) -> None:
    receipt = await native_weixin.adapter._handle_message(
        _message(text="echo", from_user_id="bot-id")
    )
    assert receipt.status == IngressStatus.REJECTED
    assert receipt.inbound_id is None


async def test_bot_type_message_dropped_by_message_type(native_weixin: NativeWeixin) -> None:
    receipt = await native_weixin.adapter._handle_message(
        _message(text="bot reply", message_type=2)
    )
    assert receipt.status == IngressStatus.REJECTED
    assert receipt.inbound_id is None


# -- 24h window reminder ---------------------------------------------------


async def test_inbound_message_marks_activity(native_weixin: NativeWeixin) -> None:
    adapter = native_weixin.adapter
    assert adapter._last_inbound == {}
    await adapter._handle_message(_message(text="hi"))
    assert "peer-1" in adapter._last_inbound
    activity = next(adapter._tokens._path.parent.glob("weixin_activity_*.json"))
    assert "peer-1" in json.loads(activity.read_text())["last_inbound"]


async def test_state_files_written_0600(native_weixin: NativeWeixin) -> None:
    adapter = native_weixin.adapter
    await adapter._handle_message(_message(text="hi"))
    paths = [adapter._tokens._path, *adapter._tokens._path.parent.glob("weixin_activity_*.json")]
    assert len(paths) == 2
    for path in paths:
        assert path.stat().st_mode & 0o777 == 0o600
    assert not (adapter._tokens._path.parent / "weixin_sync.json").exists()


async def test_push_failures_counted_and_reset(env: Any) -> None:
    """Task #829: consecutive sendmessage failures increment the watchdog
    counter; a success resets it and records the recovery moment."""
    transport, _captured = _transport(
        [
            # send 1: token attempt fails (stale) -> tokenless retry also fails
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            # send 2 (token already dropped): tokenless fails
            httpx.Response(200, json={"ret": -2, "errmsg": "prepare failed"}),
            # send 3: success
            httpx.Response(200, json={"ret": 0, "message_id": "m1"}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = WeixinAdapter(FakeCore(), client=client)
        adapter._configured = True
        adapter._user_id = "owner-1"
        adapter._tokens.set("owner-1", "ctx-token")  # token exists -> stale retry path
        with pytest.raises(RuntimeError):
            await adapter.send("owner-1", "hello")
        # one failed send call (token attempt + tokenless retry) = 1 failure
        assert adapter.push_failures == 1
        assert adapter.push_failed_at is not None
        with pytest.raises(RuntimeError):
            await adapter.send("owner-1", "still broken")
        assert adapter.push_failures == 2  # consecutive, now past the threshold
        await adapter.send("owner-1", "again")
        assert adapter.push_failures == 0
        assert adapter.push_recovered_at is not None


async def test_creation_uses_provider_event_identity_across_adapter_restart(
    native_weixin: NativeWeixin,
) -> None:
    payload = _message(text="spawn:go", message_id="1")
    original = await native_weixin.adapter._handle_message(payload)
    replacement = WeixinAdapter(native_weixin.core, client=native_weixin.adapter._http)
    native_weixin.core.register(replacement)
    assert await replacement._handle_message(payload) == original
    different = await replacement._handle_message(_message(text="spawn:go", message_id="2"))
    assert original.status == different.status == IngressStatus.UNCERTAIN
    assert original.attempt_id != different.attempt_id


@pytest.mark.parametrize("text", ["same intent text", "spawn:go"])
async def test_distinct_provider_ids_preserve_identical_content(
    native_weixin: NativeWeixin, text: str
) -> None:
    adapter = native_weixin.adapter
    one = await adapter._handle_message(_message(text=text, message_id="1"))
    two = await adapter._handle_message(_message(text=text, message_id="2"))
    assert await adapter._handle_message(_message(text=text, message_id="1")) == one
    assert one.id != two.id
    if text == "spawn:go":
        assert one.attempt_id != two.attempt_id
    else:
        assert one.inbound_id != two.inbound_id


async def test_provider_identity_retains_sender_scope(native_weixin: NativeWeixin) -> None:
    adapter = native_weixin.adapter
    one = await adapter._handle_message(
        _message(text="same", from_user_id="peer-one", message_id="1")
    )
    two = await adapter._handle_message(
        _message(text="same", from_user_id="peer-two", message_id="1")
    )
    assert one.id != two.id
    assert (
        await adapter._handle_message(
            _message(text="same", from_user_id="peer-one", message_id="1")
        )
        == one
    )


@pytest.mark.parametrize("identified_first", [True, False])
async def test_missing_id_heuristic_does_not_share_provider_identity(
    native_weixin: NativeWeixin, identified_first: bool
) -> None:
    ids = ["1", ""] if identified_first else ["", "1"]
    accepted: list[IngressReceipt] = []
    for provider_id in ids * 2:
        if provider_id:
            accepted.append(
                await native_weixin.adapter._handle_message(
                    _message(text="same", message_id=provider_id)
                )
            )
        else:
            with pytest.raises(ValueError, match="qualified provider"):
                await native_weixin.adapter._handle_message(
                    _message(text="same", message_id=provider_id)
                )
    assert accepted[0] == accepted[1]


@pytest.mark.parametrize("text", ["same chat", "spawn:go"])
async def test_poll_replay_deduplicates_events_without_merging_identical_text(
    native_weixin: NativeWeixin, text: str
) -> None:
    first, second = _message(text=text, message_id="1"), _message(text=text, message_id="2")
    native_weixin.provider_responses.extend(
        [
            _updates(first, second, sync_buf="after-one"),
            _updates(first, second, sync_buf="after-two"),
        ]
    )
    cursor, timeout = await native_weixin.adapter._poll_once("", 35000)
    assert cursor == "after-one"
    first_result = await native_weixin.adapter._handle_message(first)
    cursor, _ = await native_weixin.adapter._poll_once(cursor, timeout)
    assert cursor == "after-two"
    assert await native_weixin.adapter._handle_message(first) == first_result
    assert (await native_weixin.adapter._handle_message(second)).id != first_result.id


async def test_missing_id_heuristic_is_memory_only_across_restart(
    native_weixin: NativeWeixin,
) -> None:
    payload = _message(text="same", message_id="")
    for _ in range(2):
        adapter = WeixinAdapter(native_weixin.core, client=native_weixin.adapter._http)
        native_weixin.core.register(adapter)
        with pytest.raises(ValueError, match="qualified provider"):
            await adapter._handle_message(payload)
