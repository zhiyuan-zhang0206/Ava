"""Native timeline preparation and account-frozen selection transaction admission."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from base.deploy.maintenance import admission as maintenance
from services.entrypoints.im_bridge.outbound.types import (
    OutboundIntent,
    TimelineAcceptance,
    TimelineCandidate,
    timeline_source,
)
from services.entrypoints.im_bridge.types import ChatState, IMAdapter


@dataclass(frozen=True)
class SelectionAdmission:
    adapter: IMAdapter
    account_id: str
    guard: Callable[[Connection], None]


async def accept_timeline(
    core: Any,
    state: ChatState,
    agent_id: int,
    items: list[dict[str, Any]],
    *,
    replay_id: str = "",
    switch_arg: str = "",
    selection_admission: SelectionAdmission | None = None,
) -> TimelineAcceptance:
    if maintenance.quiesced():
        return TimelineAcceptance((), None, blocked=True)
    adapter = (
        selection_admission.adapter
        if selection_admission is not None
        else core.adapters[state.channel]
    )
    if selection_admission is not None and core.adapters.get(state.channel) is not adapter:
        raise ValueError("selection source adapter account is superseded")
    account = await adapter.outbound_account_id()
    if selection_admission is not None and account != selection_admission.account_id:
        raise ValueError("selection source adapter account changed")
    candidates: list[TimelineCandidate] = []
    for item in items:
        source = timeline_source(item)
        intent = None
        if source is not None:
            prepared = await adapter.prepare_timeline(render_item(item, agent_id))
            intent = OutboundIntent(
                channel=state.channel,
                chat_id=state.chat_id,
                agent_id=agent_id,
                source=source,
                prepared=prepared,
                replay_id=replay_id,
            )
        candidates.append(TimelineCandidate(item, intent))
    acceptance = await asyncio.to_thread(
        core.outbound_store.accept,
        state.channel,
        account,
        state.chat_id,
        agent_id,
        candidates,
        replay_id=replay_id,
        switch_arg=switch_arg,
        guard=selection_admission.guard if selection_admission is not None else None,
    )
    if acceptance.watermark is not None and acceptance.selected_agent_id == agent_id:
        core._last_pushed[(state.channel, state.chat_id, agent_id)] = acceptance.watermark
    return acceptance


def render_item(it: dict[str, Any], agent_id: int | None = None) -> str:
    """One pushed line: the human's own words or the agent's text output,
    tagged so the reader always knows who is speaking (the user's format:
    ``[User]`` / ``[Ava #<id>]``)."""

    payload = (it.get("payload") or "").strip()
    kind = it.get("kind", "")
    if kind == "inbound_chat":
        return f"[User] {payload}"
    if kind == "agent_chat":
        who = f"Ava #{agent_id}" if agent_id is not None else "Ava"
        return f"[{who}] {payload}"
    return f"[{kind}] {payload}"


async def sync_selection(core: Any, state: ChatState) -> int | None:
    """Account-bound adapters cannot import a peer-only legacy selection."""
    if maintenance.quiesced():
        raise RuntimeError("IM selection is held during maintenance")
    adapter = core.adapters[state.channel]
    account = await adapter.outbound_account_id()
    if adapter.selection_requires_account_proof:
        selected = await asyncio.to_thread(
            core.outbound_store.native_selection, state.channel, account, state.chat_id
        )
        if core.adapters.get(state.channel) is not adapter or selected is None:
            core._hold_selection(state)
            return None
    else:
        selected = await asyncio.to_thread(
            core.outbound_store.selection,
            state.channel,
            account,
            state.chat_id,
            state.current_agent_id,
        )
    core._apply_selection(state, selected)
    return selected


def hold_selection(core: Any, state: ChatState) -> None:
    """Discard derived runtime state without deleting historical selection or journals."""
    state.current_agent_id = None
    task = core._subscriptions.pop((state.channel, state.chat_id), None)
    if task is not None:
        task.cancel()
    core._stop_typing(state)


def register_adapter(core: Any, adapter: IMAdapter) -> None:
    previous = core.adapters.get(adapter.channel)
    if (
        previous is not None
        and previous is not adapter
        and adapter.selection_requires_account_proof
    ):
        for (channel, _chat), state in core.chats.items():
            if channel == adapter.channel:
                hold_selection(core, state)
    core.adapters[adapter.channel] = adapter


def truncate(text: str, limit: int) -> str:
    """Clip to the character limit, with ASCII ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def selection_account(core: Any, state: ChatState, scope: SelectionAdmission | None) -> str:
    return (
        scope.account_id
        if scope is not None
        else await core.adapters[state.channel].outbound_account_id()
    )


def apply_account_selection(
    core: Any, state: ChatState, agent_id: int | None, scope: SelectionAdmission | None
) -> None:
    if scope is None or core.adapters.get(state.channel) is scope.adapter:
        core._apply_selection(state, agent_id)
