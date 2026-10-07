"""Canonical qualified provider identity; never text or time deduplication."""

import hashlib
import json
from typing import cast
from urllib.parse import urlsplit

from services.entrypoints.im_bridge.ingress.types import ProviderSource


def uint64_message_id(provider_id: object) -> str | None:
    """Canonical positive iLink uint64, without float or boolean coercion."""
    if type(provider_id) is int:
        value = provider_id
    elif (
        isinstance(provider_id, str)
        and provider_id.isascii()
        and provider_id.isdigit()
        and len(provider_id) <= 20
    ):
        value = int(provider_id)
    else:
        return None
    return str(value) if 0 < value < 2**64 else None


def provider_namespace(base_url: str) -> str | None:
    """Qualify the configured HTTPS provider endpoint without credential components."""
    try:
        endpoint = urlsplit(base_url)
        host, port = endpoint.hostname, endpoint.port
        if (
            endpoint.scheme != "https"
            or not host
            or endpoint.username is not None
            or endpoint.password is not None
        ):
            return None
        if endpoint.query or endpoint.fragment:
            return None
        host = f"[{host}]" if ":" in host else host
        authority = host.lower() + (f":{port}" if port not in (None, 443) else "")
        return f"https://{authority}{endpoint.path.rstrip('/')}"
    except ValueError:
        return None


def source_chat_key(source: ProviderSource) -> str:
    material = json.dumps(
        [source.namespace, source.account_id, source.sender_id, source.message_id]
    )
    return "weixin-chat-v1:" + hashlib.sha256(material.encode()).hexdigest()


def validate_message_items(items: list[dict[str, object]]) -> None:
    """Known iLink item enums; unsupported known media remains explicit terminal content."""
    for item in items:
        kind = item.get("type")
        if type(kind) is not int or kind not in (0, 1, 2, 3, 4, 5, 11, 12):
            raise ValueError("Weixin provider item type is unknown")
        for field in ("text_item", "voice_item"):
            data = item.get(field)
            if data is not None and (
                not isinstance(data, dict)
                or (
                    cast(dict[str, object], data).get("text") is not None
                    and not isinstance(data["text"], str)
                )
            ):
                raise TypeError("Weixin text/transcript must be an optional string")


def poll_result_codes(response: dict[str, object]) -> tuple[int, int | None]:
    ret, errcode = response.get("ret"), response.get("errcode")
    if type(ret) is not int or (errcode is not None and type(errcode) is not int):
        raise TypeError("Weixin provider response requires an integer ret and optional errcode")
    return ret, errcode


def poll_update_fields(
    response: dict[str, object], timeout_ms: int
) -> tuple[list[dict[str, object]], str | None, int]:
    suggested = response.get("longpolling_timeout_ms")
    if suggested is not None:
        if type(suggested) is not int or suggested <= 0:
            raise TypeError("Weixin long-poll timeout must be a positive integer")
        timeout_ms = suggested
    new_buf = response.get("get_updates_buf")
    if new_buf is not None and not isinstance(new_buf, str):
        raise TypeError("Weixin continuation token must be an opaque string")
    raw_msgs = response.get("msgs")
    if not isinstance(raw_msgs, list):
        raise TypeError("Weixin updates must contain a message list")
    messages = cast(list[object], raw_msgs)
    if any(not isinstance(message, dict) for message in messages):
        raise TypeError("Weixin provider message must be an object")
    return cast(list[dict[str, object]], messages), new_buf, timeout_ms
