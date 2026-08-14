# Per-model tuning: what the evidence actually supports

PR #811 built the per-model registry and left every `ModelTuning` field `None`.
This is the decision record for filling it — what got a value, what deliberately
did not, and the three mechanism changes the sweep forced.

The rule applied throughout: **a value needs a first-party source or a
first-party curve.** "Plausible", "matches the vibe of the other models", and
third-party benchmarks alone were not enough. Fields that could not clear that
bar stay `None` and keep the shared `DEFAULT_TUNING` floor. That is why the
resulting table is sparse — sparseness is the finding, not an omission.

## Decision 1: the compaction threshold needs an absolute ceiling, not just a fraction

`auto_compact_fraction` expresses the threshold as a share of the model's
**advertised** window. Advertised windows went from 128K to 1M over two years —
roughly 8x — while measured effective context did not move with them. So one
constant means two different things: 0.8 is a defensible 160K on a 200K model
and an implausible 800K on a 1M one, and every vendor window inflation silently
loosens the threshold again while real reliability stays flat.

The decisive evidence is not a benchmark. It is that **every lab that published
what its own long-horizon agent does triggers on an absolute token count**:

| Lab | Their own agent's trigger | Source |
|---|---|---|
| Anthropic | 150,000 input tokens (Messages API compaction default; min configurable 50,000) | https://platform.claude.com/docs/en/build-with-claude/compaction |
| Anthropic | 100,000 input tokens (context editing, `clear_tool_uses_20250919` default) | same page |
| OpenAI | ~244,800 — Codex computes `(context_window * 9) / 10` and feeds it 272,000, not the advertised 1.05M | https://github.com/openai/codex/issues/31860 |
| Moonshot | 300,000 (their BrowseComp agent, "the context-compaction strategy used in the Claude model cards") | https://www.kimi.com/blog/kimi-k3 |
| DeepSeek | 512,000 (max context on every agentic eval harness — they do not run agents at 1M) | https://arxiv.org/pdf/2606.19348 |

Five independent labs, all absolute, all in a 100K–512K band. Meanwhile every
agent harness that ships a *fraction* sits at 0.8–0.97 — but those are survival
mechanisms, not quality choices: Claude Code's own docs describe its ~967K
Sonnet 5 value as auto-compacting "before the window fills, so a full context
window doesn't end your session", and its override knob is clamped to only go
**lower** (the docs' own example is `50`). Gemini CLI is the one that moved
toward the evidence: `DEFAULT_COMPRESSION_TOKEN_THRESHOLD` walked 0.95 -> 0.7 ->
0.5 (https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/context/chatCompressionService.ts).

**Decision:** hard threshold = `min(fraction * window, auto_compact_ceiling_tokens)`,
ceiling 0 = no cap. The ceiling is where per-model evidence lands; the fraction
stays as the window-relative floor that keeps a small-window model from ever
losing its threshold to a large absolute (the failure the fractions were
introduced to fix — the old absolute 800K was unreachable on a 200K model, so it
never compacted and the model overflowed).

**Rejected alternative — one global ceiling in the 150K–300K consensus band.**
Simpler, and the band is real. Rejected because it erases genuine per-model
differences the labs themselves assert (DeepSeek runs its own agents at 512K on
a published curve that is still 0.66 there; Anthropic triggers at 150K), and
because it would apply an un-sourced number to models with no evidence at all.
Per-model plus `None`-means-no-opinion keeps the sparseness honest.

**Rejected alternative — ceiling the hard threshold only.** The reminder would
then be able to sit *above* the forced ceiling, so it either never fires or
fires in the same turn as the compaction it exists to pre-empt. Both thresholds
are compressed by the same factor instead.

### Why Opus 5 / Fable 5 are more conservative than Sonnet 5

This inverts the naive intuition (they are the stronger models, so surely they
hold context better). The mechanism is documented and has nothing to do with
model strength:

> Claude Sonnet 5, Sonnet 4.6, Sonnet 4.5, and Haiku 4.5 have **context
> awareness**... The API injects them [`<budget:token_budget>` /
> `<system_warning>` tags]... **Claude Opus 4.7 and later Opus models, Claude
> Fable 5, and Claude Mythos 5 don't receive these injected tags.**

https://platform.claude.com/docs/en/build-with-claude/context-windows.md

Sonnet 5 and Haiku 4.5 manage their own tail; Opus 5 and Fable 5 cannot see the
budget at all, so this harness is the only thing managing it. Hence 100,000 for
Opus 5 / Fable 5 vs 150,000 for Sonnet 5 / Haiku 4.5.

**The 100,000 is marked PRIOR in the registry, and that marking is load-bearing.**
No per-length curve exists for Opus 5 or Fable 5 in any public benchmark —
ContextArena, the MRCR-v2 board, fiction.livebench, and AA-LCR all lack them.
This is the single largest evidence gap in the sweep and it lands on Ava's two
most-used models. The *direction* is documented; the *number* is Anthropic's
other first-party absolute (the context-editing trigger) rather than a
measurement. Revisit if a curve is ever published.

Anthropic's own counter-warning is recorded here deliberately, because this is a
real product-level disagreement and not a solved question: "overly aggressive
compaction can result in the loss of subtle but critical context"
(https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).
The interval between 0.15 and Claude Code's 0.967 is genuinely contested.

## Decision 2: the inter-chunk timeout was manufacturing failures

`llm_stream_inter_chunk_timeout_seconds` was 10. Claude Code
(`API_FORCE_IDLE_TIMEOUT`, documented as "the 5-minute idle timeout that aborts a
streaming model response when no bytes arrive",
https://code.claude.com/docs/en/env-vars) and Codex CLI (`stream_idle_timeout_ms`,
https://learn.chatgpt.com/docs/config-file/config-reference) both ship **300** for
the same parameter. Ava was 30x tighter.

Three independent mechanisms make long mid-stream silence normal rather than
faulty: Anthropic's default `display: "omitted"` thinking sends **no**
`thinking_delta` events at all (the block opens, gets one `signature_delta`, and
closes); the streaming docs acknowledge "delays between streaming events while
the model is working" during tool use; and DeepSeek explicitly permits a
10-minute queue emitting only SSE keep-alive comments. Set to 300 for every
model.

## Decision 3: TTFT depends on where the timer stops — so we checked first

The sweep flagged this as a one-line question with a large blast radius either
way, and refused to recommend numbers until it was answered. Answer, read out of
the installed provider bindings:

**Ava's TTFT timer stops at the first SSE event that LangChain converts into a
chunk — not at the first content delta.** For the Anthropic protocol (claude +
deepseek) that is `message_start`, which langchain-anthropic turns into an empty
chunk carrying `model_name`. For GPT on the Responses API that is
`response.created`, which langchain-openai converts (it matches a branch and
therefore does not hit the `return None` fallthrough). Both are protocol
preambles that arrive **before** reasoning.

Two consequences, and the tuning follows them rather than the raw benchmark
numbers:

1. On Claude and GPT, thinking time is **not** inside TTFT — it is inter-chunk
   silence. So the Artificial Analysis "time to first token" figures (sol max
   149s, DeepSeek first-answer-token 66s vs first chunk 1.71s) do **not** justify
   raising TTFT on those models; decision 2 already covers them. Their TTFT stays
   at the shared 30.
2. Gemini and the chat-completions-style providers have **no** protocol preamble,
   so their first chunk really is the first content or thought. Thinking time
   lands inside TTFT there.

TTFT was therefore raised only where the evidence is about the pre-first-byte
window specifically:

- **deepseek 600** — DeepSeek documents queueing up to 10 minutes emitting only
  SSE *comment* frames (`: keep-alive`), which no SDK surfaces as a chunk, then
  closing the connection. 600 matches the server's own cutoff.
  https://api-docs.deepseek.com/quick_start/rate_limit/
- **kimi-k3 120** — the recorded failure in PR #496: the SDK retries a 429
  internally, gets a 200, but the overloaded engine never starts streaming.
- **gemini-3.1-pro-preview 90** — no preamble on the wire, defaults to
  `thinking_level=high`, cannot drop to `minimal`, and 17s+ first-output was
  observed during a Vertex degradation.
- **claude-fable-5 120** — same silent-SDK-retry mechanism as Kimi, reached via a
  different door: it is the only roster model with 25–40% of the others'
  ITPM/OTPM at every tier.

## Decision 4: reasoning_effort needs exactly two values

Each vendor's documented default already equals what Ava wants — Anthropic
`high` (and the docs state `""` is *exactly* equivalent to omitting the
parameter), OpenAI `medium` (which is also where OpenAI files agentic coding),
Gemini flash `medium` / pro `high`, Kimi K3 `max`, GLM-5.2 `max`, Grok `high`.
Leaving those `None` is not laziness: it means Ava automatically follows a vendor
that changes its own default.

DeepSeek is the exception. It auto-promotes to `max` only for harnesses it
recognizes — the docs name Claude Code and OpenCode — and Ava is not on that
list, while DeepSeek's own report has max beating high on **every** agentic
metric. So the promotion has to be explicit.

> **Amended 2026-08-01** (user decision, task #568): the values below are now
> PINNED in each model's `ModelTuning.reasoning_effort` instead of left `None`
> — the spawn picker must show a concrete default effort and pre-select it, and
> "" is not displayable. The "follow the vendor if it changes its own default"
> property is deliberately traded away; see
> `decisions/2026-08-01-spawn-picker-concrete-effort-defaults.md`.

That created a second problem worth recording: `settings.lm.deepseek_reasoning_effort`
(default `"max"`) already existed as a provider-scoped default doing exactly the
job `ModelTuning.reasoning_effort` was built for. Once the registry carried
`"max"`, the old field became **unreachable** for both DeepSeek models — a
silently dead knob. Retired it rather than leaving two sources of truth for one
value; `AVA_REASONING_EFFORT` plus the per-agent overlay cover everything it did.

`claude_thinking_budget_tokens` stays 0 everywhere. Fable/Opus/Sonnet 5 return
400 if it is set; Haiku 4.5 is the only valid target and it does **not** support
interleaved thinking, so the budget is spent once per turn before the first tool
call and never between `execute_code` calls — the marginal value in Ava's loop is
close to zero.

## Roster fact corrections

Facts, not tuning, but they poison every fraction-derived threshold so they went
first:

- **gemini-3.1-pro-preview 2,097,152 -> 1,048,576.** Three first-party Google
  pages agree on 1M (model page, thinking guide,
  https://deepmind.google/models/model-cards/gemini-3-1-pro/); every "2M" claim
  traces to speculative blogs about a rumored Ultra tier.
- **mimo-v2.5-pro 128,000 -> 1,000,000, max output 128,000.** The registry had
  the output cap filed as the window (https://mimo.mi.com/models/zh-CN/mimo-v2.5-pro,
  https://huggingface.co/XiaomiMiMo/MiMo-V2.5-Pro). This is the correction that
  flips MiMo's compaction conclusion: at a 128K window the shared 0.8 was
  harmless, at 1M it lands at 800K, well past where Xiaomi's own GraphWalks
  numbers collapse (BFS 0.56 at 512K -> 0.37 at 1M).
- **kimi-k3 streaming False -> True.** The old default rested on "streaming
  returns ~40% 429 while non-streaming consistently succeeds", carried only in
  code comments. `git log -S` finds no incident behind it: PR #510 asserts the
  number with no artifact, and PR #496 (which predates it) records only the 429
  handling. Against it: Moonshot's own troubleshooting page recommends
  `stream=True` precisely to reduce connection errors, because without streaming
  the server withholds the response header until generation completes and
  intermediate gateways cut the wait. And the two paths never ran under the same
  clock — streaming was bounded by a 30s TTFT, the non-streaming fallback by
  `llm_non_streaming_fallback_timeout_seconds` = 600s. A 20x gap is sufficient on
  its own to produce the observed asymmetry. **This one needs post-rollout
  validation**; if K3 429 rates rise, the flip is the first thing to revert.

## What stays unset, and why that is the finding

- **All 10 prompt-behavior fields** (`prompt_*_enabled`,
  `agent_communication_style`, `agent_reflection_enabled`). These are Ava's own
  prompt-section switches. No external evidence about them can exist; only an
  internal eval can set them.
- **glm-5.2 compaction.** Z.ai publishes zero per-length data — no needle, no
  MRCR, no RULER, no LongBench, only qualitative marketing. Third-party curves
  exist and are unflattering, but a threshold set from third-party data alone
  would not meet the bar the rest of this table was held to. Left on the shared
  default deliberately.
- **grok-4.5 capability side.** Absent from every by-length benchmark, and its
  own family contradicts itself across benchmarks (grok-4.20 has the worst
  ContextArena curve of any frontier model; grok-4-0709 scores near the top of
  fiction.livebench). The 0.4 in the registry is an **economic** anchor — the
  documented 200K point where the whole request reprices at 2x — and the
  registry comment says so explicitly. No capability claim is made.
- **Exact TTFT seconds anywhere.** No vendor publishes a TTFT SLA or p50/p99.
  Every number above is a hedge against a documented *silence* mechanism, not a
  latency measurement.
- **Streaming keepalive cadence.** Not documented by a single vendor. Anthropic
  says only "any number of ping events". Nothing here may depend on heartbeat
  rhythm.
- **mimo ultraspeed inter-chunk.** The sweep suggested tightening it to 5 on the
  strength of the ~1000 t/s throughput claim. Not taken: it is an inference from
  a marketing number on an application-gated beta, and it moves against decision
  2 — reintroducing exactly the false-positive class this work removed.

## Sources not usable for this, recorded so nobody re-runs the search

- **fiction.livebench** — latest entry is `kimi-k2.5` (2026-01) and the columns
  stop at 192k. It contains no 2026-generation model on the roster.
- **RULER** — latest entry 2025-07, mostly self-reported open models.
- **NoLiMa** — 2025-generation only; useful for the *shape* (11 of 13 models
  claiming >=128K drop below half their baseline by 32K; two-hop effective length
  is about half of one-hop) but not for per-model values.
- **ContextArena** — the one third-party source with 2026 models and by-length
  curves. Used as corroboration, never as the sole basis for a value. Its
  `claude-opus-4.7` row is non-monotonic (1.4 at 64–128K, back to 8.8 at 256K)
  which is a harness-failure signature; that row is excluded.

<!-- Superseded by: decisions/2026-07-31-flat-compact-thresholds.md — the
per-model compact fractions and ceilings decided here were replaced by one flat
rule (soft 30% / hard 40% of each model's own window). The evidence table above
is unchanged and remains the record of what each lab publishes; the mechanism
(fraction + optional absolute ceiling) also survives, with the ceiling now unused
across the roster. -->
