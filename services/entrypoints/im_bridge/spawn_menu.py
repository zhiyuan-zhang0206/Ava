from __future__ import annotations

import logging

from services.entrypoints.im_bridge import copy
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.types import ChatState, Reply, SpawnDraft

_log = logging.getLogger("services.entrypoints.im_bridge.spawn_menu")


def _spawn_button(draft: SpawnDraft) -> tuple[str, str]:
    """The summary [Spawn] button every menu layer carries: the current
    selections (or "default") so the layer is completable as-is (user
    ruling: the button shows the selection summary)."""

    default = copy.SPAWN_BUTTON_DEFAULT_VALUE
    summary = (
        f"{copy.SPAWN_BUTTON_SUMMARY_PREFIX}{draft.model or default} / {draft.effort or default}"
    )
    return (summary, "spawn:go")


class SpawnMenuMixin:
    """Spawn-menu methods mixed into IMBridgeCore."""

    gateway: GatewayClient

    async def _cmd_spawn(self, state: ChatState) -> Reply:
        state.spawn_draft = SpawnDraft()
        return await self._spawn_layer_model(state)

    async def _spawn_layer_model(self, state: ChatState) -> Reply:
        draft = state.spawn_draft or SpawnDraft()
        state.spawn_draft = draft
        data = await self.gateway.list_models()
        models = sorted(data.get("models", {}).keys())
        if not models:
            return Reply(copy.SPAWN_NO_MODELS)
        buttons = [(model, f"spawn:model:{model}") for model in models]
        return Reply(copy.SPAWN_LAYER_MODEL, buttons=[*buttons, _spawn_button(draft)])

    async def _spawn_layer_effort(self, state: ChatState) -> Reply:
        draft = state.spawn_draft or SpawnDraft()
        state.spawn_draft = draft
        options = await self._effort_options(draft.model)
        buttons: list[tuple[str, str]] = [
            (copy.SPAWN_BUTTON_PROVIDER_DEFAULT, "spawn:effort:"),
            *[(f"effort: {o}", f"spawn:effort:{o}") for o in options],
        ]
        return Reply(copy.SPAWN_LAYER_EFFORT, buttons=[*buttons, _spawn_button(draft)])

    async def _effort_options(self, model: str | None) -> list[str]:
        """The model's own effort menu, or a sane generic set."""

        if model:
            try:
                data = await self.gateway.list_models()
                info = data.get("models", {}).get(model, {})
                options = info.get("reasoning_effort_options")
                if options:
                    return options
            except Exception:
                _log.warning("effort options lookup failed; using generic set")
        return ["low", "medium", "high", "max"]

    async def _handle_spawn_menu(
        self, state: ChatState, text: str, *, idempotency_key: str | None = None
    ) -> Reply | list[Reply]:
        """One tap in the spawn menu: record the selection, render the next
        layer; ``spawn:go`` executes with the current draft."""

        draft = state.spawn_draft or SpawnDraft()
        state.spawn_draft = draft
        parts = text.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""
        if action == "model":
            draft.model = value or None
            return await self._spawn_layer_effort(state)
        if action == "effort":
            draft.effort = value or None  # empty = provider default
            return await self._spawn_layer_effort(state)
        if action == "go":
            return await self._spawn_execute(state, idempotency_key=idempotency_key)
        return Reply(copy.SPAWN_UNKNOWN_ACTION)

    async def _spawn_execute(
        self, state: ChatState, *, idempotency_key: str | None = None
    ) -> Reply:
        reply, _birth_id = await self.execute_spawn(state, idempotency_key=idempotency_key)
        return reply

    async def execute_spawn(
        self, state: ChatState, *, idempotency_key: str | None = None
    ) -> tuple[Reply, int | None]:
        """Return the actual acknowledged birth ID separately from its human hint."""
        draft = state.spawn_draft
        state.spawn_draft = None
        if draft is None:
            return Reply(copy.SPAWN_NOTHING_TO_SPAWN), None
        config: dict[str, object] = {}
        if draft.model:
            config["llm_model"] = draft.model
        if draft.effort:
            config["reasoning_effort"] = draft.effort
        try:
            agent_id = await self.gateway.spawn_agent(
                config=config, idempotency_key=idempotency_key
            )
        except Exception as exc:
            _log.warning("spawn failed: %r", exc)
            return Reply(copy.SPAWN_FAILED.format(exc=exc)), None
        text = copy.SPAWNED_PLAIN.format(agent_id=agent_id)
        return Reply(
            text + copy.SPAWNED_TAP_TO_SWITCH,
            buttons=[(copy.SPAWN_SWITCH_BUTTON.format(agent_id=agent_id), f"/switch {agent_id}")],
        ), agent_id
