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
}
