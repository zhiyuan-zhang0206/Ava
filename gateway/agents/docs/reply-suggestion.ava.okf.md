---
type: doc
title: Reply Suggestion
description: Gated reply candidates shown as grey text in the empty composer, and the gateway route that produces them.
tags: [gateway, agents, lm]
---

# Reply Suggestion

After an agent's turn the web console shows model-chosen reply candidates in the empty
composer's placeholder, using its ordinary font and colour. Several candidates carry a
`[1/3]` position marker. Up/Down cycles through them; Right fills the selected text and
places the caret at its end, without the marker. Enter then sends the draft.

## Trigger (frontend)

`ui/web/src/lib/composer/use-reply-suggestion.ts` is the only writer of the candidates.
For the selected, visible agent it asks once per turn: the agent is idle, the last
conversation item is the agent's own message, and nothing new started for one second.
A new turn (busy again or a new last item), another agent, a hidden page, the user
setting `behavior.replySuggestions` (default on) turned off, or unmount aborts the request and
hides the suggestion. Opening an already-idle agent takes the same path. A settled turn is
remembered by agent and last item; an aborted one is not.

State ownership: the candidates are volatile, derived state, held in component state in
`HomeContent` (not TanStack Query, not Zustand) with the hook as its only writer;
`page.tsx` passes them to `Composer` as the `suggestions` prop. Because they are derived from the
busy signal, agent status, last item and tab visibility, it needs no clearing event. The
preference is read only after `useUserSettings` has loaded, so a stored "off" is never
raced by the default "on". `Composer` shows the text as its placeholder (so typing hides
them and clearing the input restores the selected candidate) and accepts on ArrowRight
in the empty input. Candidate selection is local to `Composer`, scoped to the response
array and agent. Accepting or editing does not consume candidates; only a successful
send consumes them, so clearing a failed send's draft restores the same selection.
A new response starts at the first candidate. Slash completion, IME composition,
modified arrows and arrows in nonempty drafts keep their ordinary behaviour.

## Route

`POST /api/agents/{agent_id}/reply-suggestion` answers `{suggestions, reason}`. `suggestions`
is an empty array exactly when `reason` is set: `disabled`, `agent_not_idle`, `no_agent_text`,
`not_useful`, `gate_failed`, `generation_failed`, `invalid_generation`. Every one is
a 200. An unknown agent is 404 and an unreadable checkpoint is the history read's 503.

1. The agent must exist and be idle; the cluster switch must be on.
2. `load_current_messages` reads the checkpoint and the shared timeline renderer turns it
   into items. The excerpt is the newest human message (`user` or `ui:page:*`) and the
   text of the last turn (everything after the newest inbound), cut from the end to
   `reply_suggestion_max_input_tokens`.
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

## Configuration

Cluster settings in `LmSettings`, read live by the gateway: `reply_suggestion_enabled`,
`reply_suggestion_model`, `reply_suggestion_gate_model`, `reply_suggestion_gate_threshold`,
`reply_suggestion_max_input_tokens`. At boot, with the feature on, the draft model must be a
registered chat model and the gate model a registered decision model
(see [[base/lm/docs/decision-models.ava.okf.md]]); anything else stops the gateway.

Code: `gateway/agents/reply_suggestion/` (`router.py`, `service.py`, `excerpt.py`,
`configuration.py`).

## Verification scope

Unit tests inject fake decision and chat models. They verify gating, validation, candidate
transport, caching and keyboard behaviour, including preserving model-returned counts
above three. They do not evaluate the usefulness judgement or generated reply quality;
those require a benchmark with representative conversations and actual model outputs.

Factory tests exercise the real LangChain and vendor SDKs with injected mock HTTP
transports. They verify the Responses URL, `text.format.type="json_schema"`, `strict=true`,
schema, reasoning settings and raw usage, and preserve the ordinary Anthropic request.

Live compatibility probes on 2026-10-13 UTC used the official OpenAI SDK and LangChain
against DeepSeek's endpoints. An ordinary draft with the reply schema succeeded. Adversarial
prompts also produced schema violations (wrong enum values, additional fields and non-string
array items) on Responses and Beta strict tool calls. Thus the client requests native strict
structured output as documented, but these probes do not establish a server-side constrained
decoding guarantee. Boundary validation remains required. These are protocol probes, not a
generation-quality benchmark. Official contracts:
[Responses schema output](https://api-docs.deepseek.com/api/create-response/),
[strict tool calls](https://api-docs.deepseek.com/guides/tool_calls/#strict-mode-beta).
