---
type: doc
title: Reply Suggestion
description: The gated, drafted reply shown as grey text in the empty composer, and the one gateway route that produces it.
tags: [gateway, agents, lm]
---

# Reply Suggestion

After an agent's turn the web console shows a drafted reply as grey text in the empty
composer; the right arrow key accepts it. Nothing is sent until the user sends it.

## Trigger (frontend)

`ui/web/src/lib/composer/use-reply-suggestion.ts` is the only writer of the suggestion.
For the selected, visible agent it asks once per turn: the agent is idle, the last
conversation item is the agent's own message, and nothing new started for one second.
A new turn (busy again or a new last item), another agent, a hidden page, the user
setting `behavior.replySuggestions` (default on) turned off, or unmount aborts the request and
hides the suggestion. Opening an already-idle agent takes the same path. A settled turn is
remembered by agent and last item; an aborted one is not.

State ownership: the suggestion is volatile, derived state, held in component state in
`HomeContent` (not TanStack Query, not Zustand) with the hook as its only writer;
`page.tsx` passes it to `Composer` as the `suggestion` prop. Because it is derived from the
busy signal, agent status, last item and tab visibility, it needs no clearing event. The
preference is read only after `useUserSettings` has loaded, so a stored "off" is never
raced by the default "on". `Composer` shows the text as its placeholder (so typing hides
it and clearing the input restores it) and accepts it on ArrowRight in the empty input.

## Route

`POST /api/agents/{agent_id}/reply-suggestion` answers `{suggestion, reason}`. `suggestion`
is null exactly when `reason` is set: `disabled`, `agent_not_idle`, `no_agent_text`,
`not_awaiting_reply`, `gate_failed`, `generation_failed`, `invalid_generation`. Every one is
a 200. An unknown agent is 404 and an unreadable checkpoint is the history read's 503.

1. The agent must exist and be idle; the cluster switch must be on.
2. `load_current_messages` reads the checkpoint and the shared timeline renderer turns it
   into items. The excerpt is the newest human message (`user` or `ui:page:*`) and the
   text of the last turn (everything after the newest inbound), cut from the end to
   `reply_suggestion_max_input_tokens`.
3. Gate: `decide(reply_suggestion_gate_model, ...)` asks one `YesNo`, "is the last
   assistant message waiting for the user". Below `reply_suggestion_gate_threshold` the
   answer is `not_awaiting_reply`.
4. Draft: `reply_suggestion_model` with reasoning off (DeepSeek defaults to maximum
   effort, which makes a one-line draft run for tens of seconds). The fixed instruction
   is the system message, first in the request, so the provider's prompt cache covers it.
   An empty draft or one over 500 characters is `invalid_generation`; it is never trimmed.
5. Usage: `llm_usage` rows with `usage_kind` `reply_suggestion_gate` and `reply_suggestion`,
   both attributed to the agent (`for_agent_id`).

A failed gate or draft is logged as a warning (`reply_suggestion_gate_failed`,
`reply_suggestion_draft_failed`) and answered as above; failures are not cached.
Completed results (a draft or `not_awaiting_reply`) are cached in process by agent and the
agent message's identity (`source_message_id`), so several tabs share one generation.

The route races the work against `request.is_disconnected()` and cancels it when the client
leaves, which stops the in-flight model call.

## Configuration

Cluster settings in `LmSettings`, read live by the gateway: `reply_suggestion_enabled`,
`reply_suggestion_model`, `reply_suggestion_gate_model`, `reply_suggestion_gate_threshold`,
`reply_suggestion_max_input_tokens`. At boot, with the feature on, the draft model must be a
registered chat model and the gate model a registered decision model
(see [[base/lm/docs/decision-models.ava.okf.md]]); anything else stops the gateway.

Code: `gateway/agents/reply_suggestion/` (`router.py`, `service.py`, `excerpt.py`,
`configuration.py`).
