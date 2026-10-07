"""Real core/adapter receipts determine ACK, not swallowed None or message text."""

import asyncio
import json

import psycopg
import pytest

from services.entrypoints.im_bridge.ingress.tests.conftest import NativeWeixin, message
from services.entrypoints.im_bridge.ingress.types import IngressIdentityConflictError, IngressStatus


async def test_actual_core_chat_and_unknown_slash_share_atomic_native_owner(
    native_weixin: NativeWeixin,
    db_conn: psycopg.Connection,
) -> None:
    first, slash = await asyncio.gather(
        native_weixin.adapter._handle_message(message("same", "1")),
        native_weixin.adapter._handle_message(message("/registered-skill work", "2")),
    )
    assert first.status == slash.status == IngressStatus.ACCEPTED
    assert first.inbound_id != slash.inbound_id
    assert native_weixin.gateway_requests == [], (
        "native chat never depends on an HTTP receipt or fallback"
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (native_weixin.agent_id,)
    ).fetchone() == (2,)
    replay = await native_weixin.adapter._handle_message(message("same", "1"))
    assert replay == first
    with pytest.raises(IngressIdentityConflictError):
        await native_weixin.adapter._handle_message(message("changed", "1"))


async def test_cursor_moves_only_after_native_source_retention(
    native_weixin: NativeWeixin,
    db_conn: psycopg.Connection,
) -> None:
    native_weixin.provider_responses.append(
        {
            "ret": 0,
            "msgs": [message("one", "1"), message("two", "2")],
            "get_updates_buf": "opaque-next",
        }
    )
    cursor, _ = await native_weixin.adapter._poll_once("", 35000)
    assert cursor == "opaque-next"
    assert db_conn.execute("SELECT cursor FROM weixin_ingress_cursors").fetchone() == (
        "opaque-next",
    )
    assert db_conn.execute(
        "SELECT count(*) FROM weixin_ingress_receipts WHERE status='accepted'"
    ).fetchone() == (2,)
    assert json.loads(native_weixin.provider_requests[0].content)["get_updates_buf"] == ""


@pytest.mark.parametrize("provider_id", [None, "", True, 1.0, 0, -1, 2**64, "not-an-id"])
async def test_unqualified_source_holds_cursor_without_content_hash_fallback(
    native_weixin: NativeWeixin,
    db_conn: psycopg.Connection,
    provider_id: object,
) -> None:
    native_weixin.provider_responses.append(
        {
            "ret": 0,
            "msgs": [message("never guessed", provider_id)],
            "get_updates_buf": "must-not-ack",
        }
    )
    with pytest.raises(ValueError, match="qualified provider"):
        await native_weixin.adapter._poll_once("", 35000)
    assert db_conn.execute("SELECT cursor FROM weixin_ingress_cursors").fetchone() == ("",)
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)


async def test_unproven_command_is_uncertain_and_does_not_block_later_chat(
    native_weixin: NativeWeixin,
    db_conn: psycopg.Connection,
) -> None:
    native_weixin.provider_responses.append(
        {
            "ret": 0,
            "msgs": [message("spawn:go", "1"), message("after command", "2")],
            "get_updates_buf": "after-both",
        }
    )
    cursor, _ = await native_weixin.adapter._poll_once("", 35000)
    assert cursor == "after-both"
    rows = db_conn.execute(
        "SELECT status,attempt_id,outcome_reason FROM weixin_ingress_receipts ORDER BY id"
    ).fetchall()
    assert rows[0][0] == "uncertain" and rows[0][1] is not None
    assert rows[0][2] == "spawn_owner_birth_unproven"
    assert rows[1][0] == "accepted"
    count = len(native_weixin.provider_requests)
    again = await native_weixin.adapter._handle_message(message("spawn:go", "1"))
    assert again.status == IngressStatus.UNCERTAIN
    assert len(native_weixin.provider_requests) == count, (
        "source replay does not rerun the command or human hint"
    )


async def test_same_rendered_text_with_changed_raw_source_kind_or_media_conflicts(
    native_weixin: NativeWeixin,
) -> None:
    adapter = native_weixin.adapter
    text = message("[voice transcript] hello", "21")
    accepted = await adapter._handle_message(text)
    voice = message("", "21")
    voice["item_list"] = [{"type": 3, "voice_item": {"text": "hello"}}]
    with pytest.raises(IngressIdentityConflictError, match="different immutable"):
        await adapter._handle_message(voice)
    assert await adapter._handle_message(text) == accepted
    media = message("", "22")
    media["item_list"] = [{"type": 2, "image_item": {"media": {"url": "original"}}}]
    terminal = await adapter._handle_message(media)
    assert terminal.status == IngressStatus.REJECTED
    changed = message("", "22")
    changed["item_list"] = [{"type": 4, "file_item": {"file_name": "different"}}]
    with pytest.raises(IngressIdentityConflictError, match="different immutable"):
        await adapter._handle_message(changed)


async def test_session_token_rotation_does_not_change_business_identity(
    native_weixin: NativeWeixin,
) -> None:
    payload = message("same", "23")
    payload["context_token"] = "first-private-token"  # noqa: S105
    accepted = await native_weixin.adapter._handle_message(payload)
    payload["context_token"] = "rotated-private-token"  # noqa: S105
    payload["create_time_ms"] = 999
    assert await native_weixin.adapter._handle_message(payload) == accepted


@pytest.mark.parametrize(
    "field,value",
    [
        ("message_type", None),
        ("message_type", True),
        ("message_type", 3),
        ("item_type", None),
        ("item_type", True),
        ("item_type", 99),
    ],
)
async def test_unknown_or_missing_source_enums_are_held_not_empty_terminal(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, field: str, value: object
) -> None:
    payload = message("requires qualified kind", "26")
    if field == "item_type":
        payload["item_list"] = [{"type": value, "text_item": {"text": "requires qualified kind"}}]
    else:
        payload[field] = value
    with pytest.raises(ValueError, match="type is unknown"):
        await native_weixin.adapter._handle_message(payload)
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)
