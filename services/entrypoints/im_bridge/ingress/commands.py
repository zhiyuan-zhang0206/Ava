"""Typed ingress verdicts from existing IM command owners, separate from human replies."""

import asyncio
import json
from typing import Any, NamedTuple

from pydantic import TypeAdapter

from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.ingress.identity import source_chat_key
from services.entrypoints.im_bridge.ingress.types import (
    IngressReceipt,
    IngressRouteKind,
    IngressStatus,
)
from services.entrypoints.im_bridge.timeline_acceptance import SelectionAdmission
from services.entrypoints.im_bridge.types import (
    ChatState,
    CoreCommand,
    Reply,
    SpawnDraft,
    parse_command,
)


class CommandVerdict(NamedTuple):
    status: IngressStatus
    reason: str | None
    result: dict[str, object] | None
    replies: tuple[Reply, ...]


def uncertain(*replies: Reply, reason: str = "command_owner_result_unproven") -> CommandVerdict:
    return CommandVerdict(IngressStatus.UNCERTAIN, reason, None, replies)


async def notice_reply_verdict(core: Any, receipt: IngressReceipt) -> CommandVerdict:
    route, source = receipt.route, receipt.source
    if route.agent_id is None or route.notice_id is None:
        raise ValueError("frozen notice reply requires its original agent and notice IDs")
    if route.reason == "reply_mode_cancel":
        cleared = core.notice_bridge.clear_reply_target(
            source.sender_id, route.agent_id, route.notice_id, account_id=route.command_account
        )
        return CommandVerdict(
            IngressStatus.ACCEPTED if cleared else IngressStatus.REJECTED,
            None if cleared else "reply_mode_superseded",
            {"notice_reply_mode_closed": True, "notice_id": route.notice_id} if cleared else None,
            (Reply("Left reply mode"),),
        )
    result = await core.notice_bridge.resolve_frozen_reply(
        route.agent_id, route.notice_id, route.text
    )
    inbound_id = result.get("inbound_id")
    if type(inbound_id) is not int or inbound_id <= 0:
        return uncertain(reason="notice_owner_inbound_unproven")
    core.notice_bridge.clear_reply_target(
        source.sender_id, route.agent_id, route.notice_id, account_id=route.command_account
    )
    return CommandVerdict(
        IngressStatus.ACCEPTED,
        None,
        {"notice_id": route.notice_id, "agent_id": route.agent_id, "inbound_id": inbound_id},
        (Reply(copy.REPLY_SENT),),
    )


async def spawn_verdict(core: Any, state: ChatState, receipt: IngressReceipt) -> CommandVerdict:
    route, text, key = receipt.route, receipt.route.text, source_chat_key(receipt.source)
    state.spawn_draft = (
        TypeAdapter(SpawnDraft).validate_json(json.dumps(route.spawn_draft))
        if route.spawn_draft is not None
        else None
    )
    if text.split(":", 2)[1] == "go":
        reply, birth_id = await core.execute_spawn(state, idempotency_key=key)
        if type(birth_id) is int and birth_id > 0:
            return CommandVerdict(IngressStatus.ACCEPTED, None, {"agent_id": birth_id}, (reply,))
        return uncertain(reply, reason="spawn_owner_birth_unproven")
    menu: Reply | list[Reply] = await core._handle_spawn_menu(state, text, idempotency_key=key)
    return uncertain(
        *(menu if isinstance(menu, list) else [menu]),
        reason="spawn_menu_owner_result_unproven",
    )


async def execute_ingress_command(
    core: Any,
    state: ChatState,
    receipt: IngressReceipt,
    *,
    selection_admission: SelectionAdmission,
) -> CommandVerdict:
    route, source = receipt.route, receipt.source
    key = source_chat_key(source)
    if route.kind == IngressRouteKind.NOTICE_REPLY:
        return await notice_reply_verdict(core, receipt)
    text = route.text
    if text.startswith("notice:"):
        hint = await core.notice_bridge.handle_callback(
            source.sender_id, text, account_id=route.command_account
        )
        return uncertain(
            *(Reply(hint),) if hint is not None else (),
            reason="notice_callback_owner_result_unproven",
        )
    if text.startswith("/notice"):
        argument = text[len("/notice") :].strip()
        hint = (
            await core.notice_bridge.list_queue()
            if argument == "list"
            else core.notice_bridge.cmd_notice(argument)
        )
        return uncertain(
            *(Reply(hint),) if hint is not None else (),
            reason="notice_command_owner_result_unproven",
        )
    if text.startswith("spawn:"):
        return await spawn_verdict(core, state, receipt)
    command, argument = parse_command(text)
    if command is None:
        raise ValueError("unknown slash commands must be admitted through the chat owner")
    replies: Reply | list[Reply] | None = await core._handle_command(
        state, text, key, replay_id=key, selection_admission=selection_admission
    )
    hints = (
        tuple(replies if isinstance(replies, list) else [replies]) if replies is not None else ()
    )
    if command == CoreCommand.SWITCH:
        accepted: dict[str, object] | None = await asyncio.to_thread(
            core.outbound_store.replay_result,
            "weixin",
            route.command_account,
            source.sender_id,
            key,
            argument.strip(),
        )
        if accepted is not None:
            return CommandVerdict(
                IngressStatus.ACCEPTED,
                None,
                accepted,
                hints,
            )
        return uncertain(*hints, reason="switch_owner_receipt_unproven")
    # Existing display/menu owners may turn lookup failures into hints. A Reply
    # without an explicit business result is retained honestly, never parsed.
    return uncertain(*hints)
