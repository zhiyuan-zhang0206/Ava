"""IM bridge delivery-health events."""

from __future__ import annotations

from typing import Literal, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class ImPushFailed(TypedDict):
    """`im_push_failed` payload — services/im_bridge/push_watchdog.py.

    One event per outbound send whose single retry also failed. `failures` is
    the adapter's consecutive-failure count (weixin: the iLink context_token
    expired; only the user's own message refreshes it)."""

    channel: str
    failures: int


class ImFeishuOwnerSeedFailed(TypedDict):
    """`im_feishu_owner_seed_failed` payload — services/im_bridge/adapters/feishu.py.

    Boot could not restore the feishu owner chat from the persisted switch
    state: `no_source` (no feishu chat recorded) or `ambiguous` (`chats` > 1);
    feishu notifications stay blind until the user messages the bot."""

    reason: Literal["no_source", "ambiguous"]
    chats: int


EVENTS: dict[str, EventSpec] = {
    "im_push_failed": telemetry_event(
        "im_push_failed",
        "an IM bridge outbound send failed after its single retry (failures = the "
        "adapter's consecutive-failure count)",
        payload=ImPushFailed,
        tier="anomaly",
    ),
    "im_feishu_owner_seed_failed": telemetry_event(
        "im_feishu_owner_seed_failed",
        "the feishu owner chat could not be restored at bridge boot; feishu "
        "notifications are blind until the user messages the bot",
        payload=ImFeishuOwnerSeedFailed,
        tier="anomaly",
    ),
}
