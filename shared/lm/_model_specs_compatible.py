"""Model specifications for OpenAI-compatible providers and legacy IDs."""

from __future__ import annotations

from shared.lm._model_registry_types import (
    _CLAUDE_ADAPTIVE_EFFORT,
    ModelSpec,
    ModelTuning,
)

COMPATIBLE_MODELS: dict[str, ModelSpec] = {
    # -- mimo --
    "mimo-v2.5-pro": ModelSpec(
        provider="mimo",
        spawnable=True,
        # Xiaomi's model page and the HF card both say 1M context / 128K max
        # output — the old 128,000 window was the OUTPUT cap filed as the
        # window, which understated the window ~8x and made the compact
        # threshold unreachable in practice.
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2024-12",
        effort_levels=("none", "high"),  # body-level thinking on/off only
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): the "high" rung IS the provider
            # default — thinking is on unless explicitly disabled
            # (shared/lm/_effort.py:mimo_extra_body).
            reasoning_effort="high",
        ),
    ),
    "mimo-v2.5-pro-ultraspeed": ModelSpec(
        provider="mimo",
        spawnable=True,
        # Same 1T/42B weights as Pro, served on the TileRT stack — same window
        # and output cap; only throughput and price differ.
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2024-12",
        effort_levels=("none", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): "high" = provider default (see
            # mimo-v2.5-pro).
            reasoning_effort="high",
            # Xiaomi omits this variant from the published RPM/TPM table
            # entirely and gates it behind an application ("limited slots"),
            # i.e. its serving capacity is self-declared scarce.
            llm_retry_max_attempts=10,
        ),
    ),
    # -- kimi --
    "kimi-k3": ModelSpec(
        provider="kimi",
        spawnable=True,
        context_window=1_048_576,
        knowledge_cutoff="2025-12",
        model_identity="You are running on Kimi K3 (Moonshot).",
        effort_levels=("low", "high", "max"),
        # Streams (the registry default). The former streaming=False carried
        # "streaming returns ~40% 429" — no incident record backs the asymmetry,
        # and Moonshot's own troubleshooting page recommends stream=True
        # precisely to avoid connection errors (non-streaming makes the server
        # withhold the response header until generation completes). The
        # asymmetry is more likely an artifact of the timeouts this commit
        # fixes: the streaming path was killed by a 30s TTFT while the
        # non-streaming fallback got 600s. See
        # decisions/2026-07-25-per-model-tuning-values.md.
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Moonshot documents K3's default
            # effort as `max` (decisions/2026-07-25-per-model-tuning-
            # values.md Decision 4).
            reasoning_effort="max",
            # Moonshot's rate limits are account-wide across every key AND every
            # model, capacity for K3 was scarce enough that new subscriptions
            # were paused, and engine_overloaded_error is explicitly defined as
            # server-side capacity that upgrading your tier does not fix.
            llm_retry_max_attempts=10,
            # The recorded K3 failure (PR #496): the provider SDK retries a 429
            # internally, gets a 200, but the overloaded engine never starts
            # streaming — no bytes, so 30s TTFT fired before the retry could land.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
        media_types=frozenset({"image"}),
    ),
    # -- glm --
    "glm-5.2": ModelSpec(
        provider="glm",
        spawnable=True,
        context_window=1_000_000,
        knowledge_cutoff="2025-12",
        # GLM-5.3 docs document the shared GLM-5-series parameter values
        # low/high/max (checked 2026-08-23); keep this entry aligned with the
        # provider clamp and its gateway invariant test.
        effort_levels=("low", "high", "max"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Z.ai documents GLM-5.2's default
            # effort as `max` (decisions/2026-07-25-per-model-tuning-
            # values.md Decision 4).
            reasoning_effort="max",
            # The roster's best-documented overload history, from Z.ai's own
            # issue tracker: a single non-batch paid user logged 285 HTTP 429s in
            # one day (~50% of all requests) and a full hour at 100% failure.
            # Z.ai's own guidance for their 1305 code ("platform service overload") is to
            # lengthen the retry interval and avoid fixed-interval hammering.
            llm_retry_max_attempts=10,
        ),
    ),
    "glm-5.3": ModelSpec(
        provider="glm",
        spawnable=True,
        context_window=1_000_000,
        # Zhipu publishes no knowledge cutoff. GLM-5.3 shares GLM-5.2's base model
        # (release notes: all gains from post-training; checked 2026-08-23), so the
        # conservative glm-5.2 value carries over — erring early is the safe direction.
        knowledge_cutoff="2025-12",
        # docs.z.ai/guides/llm/glm-5.3: reasoning_effort low/high/max, default max.
        effort_levels=("low", "high", "max"),
        # docs.z.ai/guides/overview/migrate-to-glm-new + live check 2026-08-27:
        # GLM-5.3 always thinks — thinking.type=disabled is rejected with error
        # code 1210: "this model always thinks, disabling is unsupported"), so the
        # glm builder warns
        # instead of sending the disabled body (kimi-k3 pattern).
        thinking_always_on=True,
        tuning=ModelTuning(
            # Z.ai documents GLM-5.3's default effort as max.
            reasoning_effort="max",
            # Same GLM-family overload history rationale as glm-5.2's entry
            # (llm_retry_max_attempts=10).
            llm_retry_max_attempts=10,
        ),
    ),
    "glm-5.3-flash": ModelSpec(
        provider="glm",
        spawnable=True,
        # docs.z.ai/guides/vlm/glm-5.3-flash: 1M-token context window.
        context_window=1_000_000,
        # Zhipu publishes no knowledge cutoff for any GLM-5 model; carries the
        # glm-5.3 entry's conservative 2025-12 estimate forward — erring early
        # is the safe direction (see glm-5.3's entry).
        knowledge_cutoff="2025-12",
        # docs.z.ai/api-reference/llm/chat-completion: "For the GLM-5.3
        # GLM-5.3-FLASH model, only the low / high / max levels are supported"
        # (default max); verified live 2026-08-27 that low is accepted.
        effort_levels=("low", "high", "max"),
        # docs.z.ai/guides/vlm/glm-5.3-flash documents native multimodal input
        # (images, videos, files) — but the OpenAI-compatible binding Ava dials
        # renders image_url blocks only today, the same conservatism as
        # qwen3.8-max (whose official modality list also includes video).
        media_types=frozenset({"image"}),
        # Same always-on thinking as glm-5.3 (docs + live 400 on
        # thinking.type=disabled, error code 1210).
        thinking_always_on=True,
        tuning=ModelTuning(
            # Z.ai documents the GLM-5.3 series' default effort as max.
            reasoning_effort="max",
            # Same GLM-family overload history rationale as glm-5.2's entry
            # (llm_retry_max_attempts=10).
            llm_retry_max_attempts=10,
        ),
    ),
    # -- qwen --
    "qwen3.8-max": ModelSpec(
        provider="qwen",
        spawnable=True,
        # Alibaba publishes 991,808 max input and 983,616 "max input (thinking
        # mode)". This roster runs with thinking on, so the lower ceiling is the
        # one an agent actually has.
        context_window=983_616,
        max_output_tokens=131_072,
        # Alibaba publishes NO training-data cutoff for any Qwen model (checked
        # 2026-08-20 across the Model Studio model pages, the pricing page and
        # the Qwen team blog) — but `_validate_registry` requires one for a
        # spawnable model, and the value only feeds the system prompt's temporal
        # boundary. This is a deliberate conservative estimate anchored on the
        # model page's own publication (2026-08-03): erring EARLY is the safe
        # direction, since an over-late cutoff makes the agent trust stale
        # knowledge as current. Replace it the day Alibaba publishes one.
        knowledge_cutoff="2026-01",
        model_identity="You are running on Qwen3.8-Max (Alibaba Qwen).",
        # No graded effort field on the compatible-mode endpoint — the knob's
        # only wire effect is the `enable_thinking` on/off switch
        # (shared/lm/_effort.py:qwen_extra_body), same binary as mimo.
        effort_levels=("none", "high"),
        tuning=ModelTuning(
            # The "high" rung IS the provider default: thinking is on for this
            # model unless `enable_thinking=false` is sent.
            reasoning_effort="high",
        ),
        media_types=frozenset({"image"}),
    ),
    "qwen3.8-27b": ModelSpec(
        provider="qwen",
        spawnable=True,
        # Same ceilings as qwen3.8-max (thinking-mode input is the binding one —
        # see the max entry); only price and weight class differ, at roughly a
        # quarter of max's input rate.
        context_window=983_616,
        max_output_tokens=131_072,
        # Same unpublished-cutoff situation as every Qwen — see qwen3.8-max.
        # Anchored on this model's own publication (2026-08-17), erring early.
        knowledge_cutoff="2026-01",
        model_identity="You are running on Qwen3.8-27B (Alibaba Qwen).",
        effort_levels=("none", "high"),
        tuning=ModelTuning(
            # Thinking on by default here too — verified live 2026-08-20 that
            # `enable_thinking: false` is honored rather than rejected.
            reasoning_effort="high",
        ),
        media_types=frozenset({"image"}),
    ),
    "qwen3.8-flash": ModelSpec(
        provider="qwen",
        spawnable=True,
        # DashScope model-info API (GET /api/v1/models, checked 2026-08-27):
        # context 1M, max input 991,808, reasoning-mode max input 983,616.
        # This roster runs with thinking on, so the lower ceiling is the one an
        # agent actually has (same convention as qwen3.8-max).
        context_window=983_616,
        max_output_tokens=131_072,
        # Same unpublished-cutoff situation as every Qwen — see qwen3.8-max.
        # Anchored on this model's own publication (2026-08-25), erring early.
        knowledge_cutoff="2026-01",
        model_identity="You are running on Qwen3.8-Flash (Alibaba Qwen).",
        # Same binary enable_thinking knob as every registered qwen model
        # (shared/lm/_effort.py:qwen_extra_body) — verified live 2026-08-27
        # that thinking is ON by default and enable_thinking=false is honored.
        effort_levels=("none", "high"),
        tuning=ModelTuning(
            # The "high" rung IS the provider default: thinking is on unless
            # enable_thinking=false is sent (same as qwen3.8-max).
            reasoning_effort="high",
        ),
        # Official request modality is Image/Text/Video (DashScope model-info
        # API), but the compatible-mode binding renders image blocks only —
        # same conservatism as qwen3.8-max's entry.
        media_types=frozenset({"image"}),
    ),
    # -- legacy / non-spawnable (facts kept for old agents) --
    "claude-opus-4-8": ModelSpec(
        provider="claude",
        context_window=200_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-sonnet-4-6": ModelSpec(
        provider="claude",
        context_window=200_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2025-08",
        effort_levels=("low", "medium", "high", "max"),  # xhigh arrived with opus-4-7
        tuning=ModelTuning(
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-opus-4-7": ModelSpec(
        provider="claude",
        max_output_tokens=128_000,
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "claude-opus-4-6": ModelSpec(
        provider="claude",
        tuning=ModelTuning(
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    # Bare alias of the dated snapshot above, kept for old agent configs.
    # Carries the same extended-thinking-only flag as the dated entry: without
    # it the factory's adaptive-thinking default would send `type: "adaptive"`,
    # which this model 400s on.
    "claude-haiku-4-5": ModelSpec(
        provider="claude",
        extended_thinking_only=True,
        tuning=ModelTuning(
            # User decision (2026-09-03): Claude family defaults this section off.
            prompt_user_tone_enabled=False,
        ),
        media_types=frozenset({"image", "pdf"}),
    ),
    "gpt-5.5": ModelSpec(
        provider="gpt",
        context_window=256_000,
        knowledge_cutoff="2025-12",
        media_types=frozenset({"image"}),
    ),
    "gpt-5.4-mini": ModelSpec(
        provider="gpt",
        context_window=256_000,
        knowledge_cutoff="2025-08",
        media_types=frozenset({"image"}),
    ),
}
