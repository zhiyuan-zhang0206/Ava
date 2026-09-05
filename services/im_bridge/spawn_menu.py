"""The /spawn layered menu — preset -> model -> effort.

Split out of ``core`` (which owns routing and the SSE push loop) so the file
stays under the per-file line ceiling. The mixin is imported by
:class:`services.im_bridge.core.IMBridgeCore`; every method operates on the
core's per-chat ``ChatState`` through the shared IM Bridge types.

Design (locked with the user, 2026-08-03):
- /spawn is a pure menu; text arguments are ignored
- each layer carries a summary [Spawn] button ("Spawn: preset / model /
  effort", "default" for anything unselected) so any layer completes
- no confirm step: tapping Spawn creates the agent immediately
- the receipt offers a "Switch to #id" button
"""

from __future__ import annotations

import logging

from services.im_bridge import copy
from services.im_bridge.gateway_client import GatewayClient
from services.im_bridge.types import ChatState, Reply, SpawnDraft

_log = logging.getLogger("services.im_bridge.spawn_menu")


def _spawn_button(draft: SpawnDraft) -> tuple[str, str]:
    """The summary [Spawn] button every menu layer carries: the current
    selections (or "default") so the layer is completable as-is (user
    ruling: the button shows the selection summary)."""

    preset = draft.preset_label if draft.preset_id is not None else copy.SPAWN_BUTTON_DEFAULT_VALUE
    summary = (
        f"{copy.SPAWN_BUTTON_SUMMARY_PREFIX}{preset} / "
        f"{draft.model or copy.SPAWN_BUTTON_DEFAULT_VALUE} / "
        f"{draft.effort or copy.SPAWN_BUTTON_DEFAULT_VALUE}"
    )
    return (summary, "spawn:go")


class SpawnMenuMixin:
    """Spawn-menu methods mixed into IMBridgeCore."""

    gateway: GatewayClient

    async def _cmd_spawn(self, state: ChatState) -> Reply:
        """/spawn: a three-layer menu (preset -> model -> effort); every
        layer is completable via the summary [Spawn] button (user ruling:
        pure menu, args ignored, no confirm step)."""

        state.spawn_draft = SpawnDraft()
        return await self._spawn_layer_preset(state)

    async def _spawn_layer_preset(self, state: ChatState) -> Reply:
        draft = state.spawn_draft or SpawnDraft()
        state.spawn_draft = draft
        presets = await self.gateway.list_presets()
        if not presets:
            return await self._spawn_layer_model(state)  # nothing to pick — next layer
        buttons: list[tuple[str, str]] = [
            (copy.SPAWN_BUTTON_NO_PRESET, "spawn:preset:none"),
            *[(f"{p.get('label') or p['name']}", f"spawn:preset:{p['id']}") for p in presets],
        ]
        return Reply(copy.SPAWN_LAYER_PRESET, buttons=[*buttons, _spawn_button(draft)])

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

    async def _handle_spawn_menu(self, state: ChatState, text: str) -> Reply | list[Reply]:
        """One tap in the spawn menu: record the selection, render the next
        layer; ``spawn:go`` executes with the current draft."""

        draft = state.spawn_draft or SpawnDraft()
        state.spawn_draft = draft
        parts = text.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""
        if action == "preset":
            if value == "none":
                draft.preset_id = draft.preset_name = draft.preset_label = None
            else:
                presets = await self.gateway.list_presets()
                picked = next((p for p in presets if str(p["id"]) == value), None)
                if picked is None:
                    return Reply(copy.SPAWN_PRESET_GONE)
                draft.preset_id = picked["id"]
                draft.preset_name = picked["name"]
                draft.preset_label = picked.get("label") or picked["name"]
            return await self._spawn_layer_model(state)
        if action == "model":
            draft.model = value or None
            return await self._spawn_layer_effort(state)
        if action == "effort":
            draft.effort = value or None  # empty = provider default
            return await self._spawn_layer_effort(state)
        if action == "go":
            return await self._spawn_execute(state)
        return Reply(copy.SPAWN_UNKNOWN_ACTION)

    async def _spawn_execute(self, state: ChatState) -> Reply:
        draft = state.spawn_draft
        state.spawn_draft = None
        if draft is None:
            return Reply(copy.SPAWN_NOTHING_TO_SPAWN)
        config: dict[str, object] = {}
        if draft.model:
            config["llm_model"] = draft.model
        if draft.effort:
            config["reasoning_effort"] = draft.effort
        try:
            agent_id = await self.gateway.spawn_agent(preset=draft.preset_name, config=config)
        except Exception as exc:
            _log.warning("spawn failed: %r", exc)
            return Reply(copy.SPAWN_FAILED.format(exc=exc))
        if draft.preset_label:
            text = copy.SPAWNED_WITH_PRESET.format(preset=draft.preset_label, agent_id=agent_id)
        else:
            text = copy.SPAWNED_PLAIN.format(agent_id=agent_id)
        return Reply(
            text + copy.SPAWNED_TAP_TO_SWITCH,
            buttons=[(copy.SPAWN_SWITCH_BUTTON.format(agent_id=agent_id), f"/switch {agent_id}")],
        )
