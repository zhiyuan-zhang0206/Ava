"""Weixin's existing adapter loop owns retained source recovery and first dispatch."""

import asyncio
import json
from dataclasses import asdict
from functools import partial
from typing import Any

from base.log import logger
from services.entrypoints.im_bridge.ingress.identity import (
    provider_namespace,
    source_chat_key,
    uint64_message_id,
)
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.ingress.types import (
    IngressBindingState,
    IngressReceipt,
    IngressRoute,
    IngressRouteKind,
    IngressStatus,
    PollBinding,
    ProviderSource,
)
from services.entrypoints.im_bridge.timeline_acceptance import SelectionAdmission
from services.entrypoints.im_bridge.types import ChatState, IMAdapter, parse_command


class WeixinIngress:
    def __init__(self, core: Any, store: WeixinIngressStore, adapter: IMAdapter) -> None:
        self.core = core
        self.store = store
        self.adapter = adapter
        self.command_states: dict[tuple[str, str, str], ChatState] = {}

    async def initialize(
        self, namespace: str, account_id: str, legacy_cursor: str | None
    ) -> PollBinding:
        binding = await asyncio.to_thread(
            self.store.initialize, namespace, account_id, legacy_cursor
        )
        if not binding.held:
            await asyncio.to_thread(self.store.recover_claims, binding)
        return binding

    async def recover_retained(self, binding: PollBinding) -> None:
        for receipt in await asyncio.to_thread(self.store.pending, binding):
            await self.dispatch(binding, receipt)

    async def check_adapter(self, binding: PollBinding) -> str:
        if self.core.adapters.get("weixin") is not self.adapter:
            raise ValueError("Weixin ingress adapter is superseded")
        account = await self.adapter.outbound_account_id()
        endpoint, account_id = json.loads(account)
        if provider_namespace(endpoint) != binding.namespace or account_id != binding.account_id:
            raise ValueError("Weixin ingress source account is superseded")
        if not self.adapter.selection_requires_account_proof:
            raise ValueError("Weixin native ingress requires account proof")
        return account

    async def capture_route(self, source: ProviderSource, text: str) -> IngressRoute:
        account = await self.adapter.outbound_account_id()
        if self.core.notice_bridge.reply_context_held(source.sender_id, text, account):
            return IngressRoute(
                kind=IngressRouteKind.TERMINAL,
                text=text,
                reason="notice_reply_mode_account_unproven",
            )
        target = self.core.notice_bridge.reply_target(source.sender_id, text, account_id=account)
        if target is not None:
            return IngressRoute(
                kind=IngressRouteKind.NOTICE_REPLY,
                text=text,
                agent_id=target[0],
                notice_id=target[1],
                command_account=account,
                reason="reply_mode_cancel" if text.startswith("/cancel") else None,
            )
        selected = await asyncio.to_thread(
            self.core.outbound_store.native_selection, "weixin", account, source.sender_id
        )
        command, _arg = parse_command(text)
        if command is not None or text.startswith(("spawn:", "notice:", "/notice")):
            context = self.command_states.setdefault(
                (source.namespace, source.account_id, source.sender_id),
                ChatState("weixin", source.sender_id),
            )
            return IngressRoute(
                kind=IngressRouteKind.COMMAND,
                text=text,
                agent_id=selected,
                command_account=account,
                spawn_draft=asdict(context.spawn_draft)
                if context.spawn_draft is not None
                else None,
            )
        return IngressRoute(kind=IngressRouteKind.CHAT, text=text, agent_id=selected)

    @staticmethod
    def provider_source(binding: PollBinding, message: dict[str, Any]) -> ProviderSource:
        sender_id = message.get("from_user_id")
        message_id = uint64_message_id(message.get("message_id"))
        if not isinstance(sender_id, str) or not sender_id.strip() or message_id is None:
            raise ValueError("Weixin ingress requires a qualified provider sender and uint64 id")
        source = ProviderSource(
            namespace=binding.namespace,
            account_id=binding.account_id,
            sender_id=sender_id.strip(),
            message_id=message_id,
        )
        state = message.get("message_state")
        if type(state) is not int or state != 2:
            raise ValueError("Weixin ingress holds incomplete or unknown provider message states")
        message_type = message.get("message_type")
        if type(message_type) is not int or message_type not in (0, 1, 2):
            raise ValueError("Weixin provider message type is unknown")
        return source

    async def accept(
        self, binding: PollBinding, message: dict[str, Any], text: str
    ) -> IngressReceipt:
        await self.check_adapter(binding)
        source = self.provider_source(binding, message)
        sender_id, message_type = source.sender_id, message.get("message_type")
        # Session/context tokens can rotate; the first original payload remains retained.
        request = {
            "text": text,
            "item_list": message.get("item_list", []),
            "message_type": message.get("message_type"),
            "room_id": message.get("room_id"),
            "chat_room_id": message.get("chat_room_id"),
            "group_id": message.get("group_id"),
        }
        previous = await asyncio.to_thread(self.store.lookup, binding, source, request)
        if previous is not None:
            if (
                previous.status == IngressStatus.RETAINED
                and binding.state == IngressBindingState.ACTIVE
            ):
                return await self.dispatch(binding, previous)
            return previous
        adopted = await asyncio.to_thread(
            self.store.quarantine_or_adopt,
            binding,
            source,
            request,
            message,
            text.strip(),
            source_chat_key(source),
            quarantine_unknown=binding.state == IngressBindingState.DRAINING,
        )
        if adopted is not None:
            return adopted
        if (
            sender_id == binding.account_id
            or message.get("message_type") == 2
            or message.get("room_id")
            or message.get("chat_room_id")
            or message.get("group_id")
            or message_type == 0
        ):
            route = IngressRoute(
                kind=IngressRouteKind.TERMINAL, text=text, reason="provider_event_filtered"
            )
        elif not text:
            route = IngressRoute(
                kind=IngressRouteKind.TERMINAL, text=text, reason="unsupported_empty_content"
            )
        else:
            route = await self.capture_route(source, text.strip())
        retained = await asyncio.to_thread(
            self.store.retain, binding, source, request, message, route, source_chat_key(source)
        )
        if retained.status == IngressStatus.RETAINED:
            return await self.dispatch(binding, retained)
        return retained

    async def selection_scope(
        self, binding: PollBinding, receipt: IngressReceipt, adapter: IMAdapter
    ) -> SelectionAdmission:
        account = await self.check_adapter(binding)
        if account != receipt.route.command_account:
            raise ValueError("Weixin command source account is superseded")
        return SelectionAdmission(
            adapter,
            account,
            partial(self.store.guard_claim_in_transaction, binding=binding, receipt=receipt),
        )

    async def recover_subscriptions(self, binding: PollBinding) -> None:
        """The active adapter loop reconstructs derived subscriptions from canonical account selection."""
        await asyncio.to_thread(self.store.refresh_binding, binding)
        account = await self.check_adapter(binding)
        adapter = self.adapter
        selected = await asyncio.to_thread(
            self.core.outbound_store.native_selections, "weixin", account
        )
        if self.core.adapters.get("weixin") is not adapter:
            return
        for (channel, chat), state in list(self.core.chats.items()):
            if channel == "weixin" and chat not in selected:
                self.core._hold_selection(state)
        for chat, agent in selected.items():
            state = self.core._get_or_create_state("weixin", chat)
            if state.current_agent_id != agent or ("weixin", chat) not in self.core._subscriptions:
                self.core._apply_selection(state, agent)

    async def recover_after_accept(self, binding: PollBinding, receipt: IngressReceipt) -> None:
        if receipt.status != IngressStatus.ACCEPTED or receipt.route.kind != IngressRouteKind.CHAT:
            return
        try:
            await self.recover_subscriptions(binding)
        except Exception:
            logger.warning("Weixin subscription recovery pending receipt={}", receipt.id)

    async def dispatch(self, binding: PollBinding, receipt: IngressReceipt) -> IngressReceipt:
        await self.check_adapter(binding)
        claimed = await asyncio.to_thread(self.store.claim, binding, receipt.id)
        if claimed is None:
            return await asyncio.to_thread(self.store.get_receipt, binding, receipt.id)
        # Only a typed owner result may claim business acceptance. Hints are separate.
        from services.entrypoints.im_bridge.ingress.commands import execute_ingress_command

        adapter = self.adapter
        try:
            selection_admission = await self.selection_scope(binding, claimed, adapter)
            context = self.command_states.setdefault(
                (claimed.source.namespace, claimed.source.account_id, claimed.source.sender_id),
                ChatState("weixin", claimed.source.sender_id),
            )
            context.current_agent_id = claimed.route.agent_id
            outcome = await execute_ingress_command(
                self.core, context, claimed, selection_admission=selection_admission
            )
            status, reason, result, replies = outcome
        except Exception:
            logger.warning(
                "Weixin command outcome unproven receipt={} reason=command_call_ambiguous",
                claimed.id,
            )
            status, reason, result, replies = (
                IngressStatus.UNCERTAIN,
                "command_call_ambiguous",
                None,
                (),
            )
        finished = await asyncio.to_thread(
            self.store.finish, binding, claimed, status, reason, result
        )
        if finished is None:
            raise RuntimeError("Weixin command outcome ownership compare-and-set rejected")
        if finished.status == IngressStatus.UNCERTAIN:
            logger.warning(
                "Weixin command uncertain receipt={} attempt={} reason={}",
                finished.id,
                finished.attempt_id,
                finished.outcome_reason,
            )
        # A failed human/provider hint never demotes business acceptance or reruns a command.
        for reply in replies:
            if self.core.adapters.get("weixin") is not adapter:
                break
            try:
                await self.core._send("weixin", finished.source.sender_id, reply, adapter=adapter)
            except Exception:
                logger.warning("Weixin ingress hint unavailable receipt={}", finished.id)
        return finished
