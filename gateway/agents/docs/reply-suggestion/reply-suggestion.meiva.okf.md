---
type: doc
title: Reply Suggestion
description: Gated reply candidates shown as grey text in the empty composer, and the gateway route that produces them.
tags: [gateway, agents, lm]
---

# Reply Suggestion

After an agent's turn the web console shows model-chosen reply candidates in the empty
composer's placeholder, using its ordinary font and colour. Several candidates carry a
`[1/3]` position marker. Up/Down moves through them, stopping at the first and last
(no wrap-around); Right fills the selected text and places the caret at its end, without
the marker. Enter then sends the draft.

## Trigger (frontend)

[[gateway/agents/docs/reply-suggestion/trigger.meiva.okf.md|Trigger (frontend)]] — the frontend hook and component state that request, own and render the candidates.

## Route

`POST /api/agents/{agent_id}/reply-suggestion` answers `{suggestions, reason}`. `suggestions`
is an empty array exactly when `reason` is set: `disabled`, `agent_not_idle`, `no_agent_text`,
`not_useful`, `gate_failed`, `generation_failed`, `invalid_generation`. Every one is
a 200. An unknown agent is 404 and an unreadable checkpoint is the history read's 503.
An unavailable model catalog returns the gateway's `model_catalog_unavailable`
503. Invalid enabled suggestion settings return `reply_suggestion_unavailable`
503, without exposing configuration details or pretending the suggestion was
simply not useful.

1. The agent must exist and be idle; the cluster switch must be on.
2. `load_current_messages` reads the checkpoint and the shared timeline renderer turns it
   into items. The excerpt is the newest human message (`user` or `ui:page:*`) and the
   text of the last turn (everything after the newest inbound), cut to
   `reply_suggestion_max_input_tokens`. The user's message has the budget's first claim:
   a long agent turn cannot evict the message the suggestion answers, and whichever part
   must be cut keeps its newest words. `excerpt.py`'s `is_human_source` mirrors
   `inboundKind` in `ui/web/src/components/timeline/model/runs.ts` (the `user` /
   `ui:page:*` split); keep the pair in sync.
3. Gate: `decide(reply_suggestion_gate_model, ...)` asks one `YesNo`, "would suggesting
   a reply help the user at this point in the conversation?". Below
   `reply_suggestion_gate_threshold` the answer is `not_useful`. This judges the value
   of suggesting a reply, not whether the agent is blocked waiting for the user.
4. Draft: `reply_suggestion_model` with reasoning off (DeepSeek defaults to maximum
   effort, which makes a one-line draft run for tens of seconds). The fixed instruction
   is the system message, first in the request, so the provider's prompt cache covers it.
   One native structured-output call submits the `DraftReplies` schema through
   `with_structured_output(method="json_schema", strict=True, include_raw=True)`, returning
   an object containing `suggestions`. The prompt specifies the task, not a JSON format.
   The model chooses both the number and content from context; there is no preferred
   count, three-candidate ceiling or question-type policy.
   The default DeepSeek provider uses its Responses endpoint for this operation because
   its Anthropic-compatible endpoint supports only `output_config.effort`. The configured
   model, key, reasoning effort, timeout and output cap are preserved; ordinary agent chat
   retains the Anthropic transport. There is no plain-text or alternate-model fallback.
   The client retains the raw message for usage accounting and rejects unfinished or
   unparsable output. Pydantic validates the parsed object, rejecting missing or additional
   fields, duplicate replies, empty arrays or strings, and candidates over 500 characters
   as `invalid_generation`. Invalid candidates are rejected as a whole, never partially
   salvaged or truncated.
5. Usage: `llm_usage` rows with `usage_kind` `reply_suggestion_gate` and `reply_suggestion`,
   both attributed to the agent (`for_agent_id`).

A failed gate or draft is logged as a warning (`reply_suggestion_gate_failed`,
`reply_suggestion_draft_failed`) and answered as above; failures are not cached.
Completed results (a draft or `not_useful`) are cached in process by agent and the
agent message's identity (`source_message_id`), so several tabs share one generation.

The route races the work against `request.is_disconnected()` and cancels it when the client
leaves, which stops the in-flight model call.
The disconnect watcher also observes work completion: Starlette's anyio cancel
scope can absorb cancellation during its disconnect poll, so cancellation alone
must not leave the watcher running after failed work ends.

## Configuration

Cluster settings in `LmSettings`, read by the gateway from the configuration snapshot
captured at its process start: `reply_suggestion_enabled`, `reply_suggestion_model`,
`reply_suggestion_gate_model`, `reply_suggestion_gate_threshold`,
`reply_suggestion_max_input_tokens`. A changed value takes effect on the next gateway
restart. The per-user `behavior.replySuggestions` preference is separate and takes effect
without one. Construction captures readers without reading models or policy. After the request
passes the enabled, idle and excerpt checks, the draft model must be a registered
chat model and the gate model a registered decision model
(see [[base/lm/docs/decision-models.meiva.okf.md]]). Validation runs before cache lookup:
a changed invalid setting cannot replay a previously cached success. One catalog
value serves the gate, draft and usage calculation for that request. Disabled
suggestions and requests without usable agent text do not acquire the catalog.

Only classified provider failures degrade to `gate_failed` or `generation_failed`.
Unclassified programming/configuration errors remain errors. The native draft
must be complete JSON before its parsed result is accepted; LangChain's parser
can repair truncated JSON, which must not turn an unfinished reply into success.
The per-call timeouts bound an enhancement whose late result is no longer useful.

Code: `gateway/agents/reply_suggestion/` (`router.py`, `service.py`, `excerpt.py`,
`configuration.py`).

## Verification scope

What the unit, factory and live-probe evidence covers — and the boundaries it does not establish — is owned by [[gateway/agents/docs/reply-suggestion/verification.meiva.okf.md|Reply suggestion verification]].
