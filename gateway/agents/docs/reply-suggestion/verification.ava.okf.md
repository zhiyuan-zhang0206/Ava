---
type: doc
title: Reply suggestion verification
description: What reply-suggestion evidence covers — gating, validation, transports — and the protocol-probe limits it does not establish.
tags: [gateway, agents, lm]
---

# Reply suggestion verification

Unit tests inject fake decision and chat models. They verify gating, validation, candidate
transport, caching and keyboard behaviour, including preserving model-returned counts
above three. They do not evaluate the usefulness judgement or generated reply quality;
those require a benchmark with representative conversations and actual model outputs.

Factory tests exercise the real LangChain and vendor SDKs with injected mock HTTP
transports. They verify the Responses URL, `text.format.type="json_schema"`, `strict=true`,
schema, reasoning settings and raw usage. Separate construction assertions verify the
ordinary Anthropic transport, endpoint, key and disabled-thinking configuration.

Live compatibility probes on 2026-10-13 UTC used the official OpenAI SDK and LangChain
against DeepSeek's endpoints. An ordinary draft with the reply schema succeeded. Adversarial
prompts also produced schema violations (wrong enum values, additional fields and non-string
array items) on Responses and Beta strict tool calls. Thus the client requests native strict
structured output as documented, but these probes do not establish a server-side constrained
decoding guarantee. Boundary validation remains required. These are protocol probes, not a
generation-quality benchmark. Official contracts:
[Responses schema output](https://api-docs.deepseek.com/api/create-response/),
[strict tool calls](https://api-docs.deepseek.com/guides/tool_calls/#strict-mode-beta).

## Relationship to Other Nodes

- [[gateway/agents/docs/reply-suggestion/reply-suggestion.ava.okf.md|Reply Suggestion]] — the feature this verification belongs to.
