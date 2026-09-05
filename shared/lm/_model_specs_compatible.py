"""Model specifications for OpenAI-compatible providers and legacy IDs."""

from __future__ import annotations

from shared.lm._model_registry_types import (
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
}
