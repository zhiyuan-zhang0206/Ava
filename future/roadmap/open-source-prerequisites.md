# Open-source / scale-out prerequisites

A holding bucket for items that are **not** built today purely because Ava is
single-user / single-host, and that become **required** the moment the project
is open-sourced and grown for adoption. The distinction matters: these are not
architectural noes (they don't violate the small-core / fail-fast charter) —
they are positioning-gated, and the positioning flips at the open-source
milestone. Listed here so they are never mistaken for the permanent deliberate-no
list.

Nothing here is worked on until the open-source decision is made. This file is
the memo to future-us about what that decision pulls in — the *feature* gaps, not
the mechanical publish steps.

## IM ingress channels (20+)

Today: Telegram is the only live channel (WeChat iLink / Feishu adapters exist but are production-disabled since 2026-08-06). IM **ingress is live**: the IM Bridge takes user commands via the IM surface (`/list` `/switch` `/status` `/spawn` `/commands` `/help` `/notice`) — ingress is *not* web-only.

At open-source time: native ingress for the IM apps users actually live in —
Slack, Discord, Telegram, WhatsApp, Signal, WeChat/Weixin, Feishu/Lark, and the
long tail — is table stakes for adoption. Competitors (OpenClaw ~20, Hermes
22-23) treat channel count as a headline feature precisely because it is a direct
driver of "I can use this where I already am" → GitHub stars. Each channel is a
gateway adapter; the shape is well-understood, the work is breadth not depth.

Why deferred, not rejected: a single-user fleet has exactly one user reachable
on one channel, so the count is pure cost now. A public project inverts that.

## Provider fallback chain

Today: single model *live per deployment*, fail-fast — if the provider is
down, the agent's view errors and a human swaps config. This is correct for
single-user (the operator is present to swap). "Single model" here describes
the default *operating* choice, not a capability ceiling — the registry
(`shared/lm/registry.py`) already backs 8 providers side by side, and any
agent can be spawned on any of them today via `config_overlay`/presets.

At open-source time: a public deployment cannot hard-fail when one upstream
provider has an outage or rate-limits. An ordered fallback chain
(DeepSeek -> Kimi / Qwen / Anthropic) becomes a real availability requirement.

Why this is *not* the banned "model fallback" non-goal at that point: the
non-goal (`conventions/non-goals.md`,
`decisions/2026-07-29-no-runtime-model-routing.md`) rejects fallback as a
way to *paper over model mistakes*, or an opaque cost/load router, for a
single operator who can just swap. For a public multi-tenant deployment,
fallback is an *availability* mechanism, not a mistake-shim — different problem,
different verdict. The non-goal stands for today's positioning; this bucket
records that it is scoped to today, not forever.

## Likely future neighbors in this bucket

Not committed, but the kind of thing that lands here when "grow the project"
becomes the goal: multi-tenant auth/isolation, per-user resource quotas, a
hosted onboarding path. Captured only so the bucket's intent is clear — it is
"things single-user-positioning lets us skip," and that set grows the moment
positioning changes.
