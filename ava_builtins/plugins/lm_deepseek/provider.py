"""DeepSeek provider plugin for Ava."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only (`build` return): keeps the chat-model stack off the
    # provider-registration path (exec-child boot, task #3633;
    # `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel

from shared.lm._effort import _clamp_effort
from shared.lm.provider_api import (
    AttachPolicy,
    BuildContext,
    PricePeriod,
    PriceRates,
    PriceTier,
    PriceWindow,
    ProviderBinding,
    register,
    require_key,
)
from shared.lm.registry import MODELS, ModelSpec, ModelTuning

# DeepSeek's anthropic-compatible endpoint. Also the single source of truth
# for model name → endpoint resolution; not written twice — to change the
# endpoint, change here.
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
_DEEPSEEK_EFFORT_LEVELS = ("high", "max")


def deepseek_wire_effort(effort: str, levels: tuple[str, ...], *, target: str) -> str | None:
    """Resolve a cross-provider effort onto DeepSeek's `output_config.effort`.

    None means "no reasoning": DeepSeek's wire vocabulary is graded levels only
    and carries no `none` variant (sending one 400s with ``unknown variant
    `none` ``), so off is the endpoint's thinking switch instead — the caller's
    job. Every other value clamps onto `levels`, which is what keeps a global
    effort this model does not accept from reaching the wire unclamped.
    """
    if effort == "none":
        return None
    return _clamp_effort(effort, levels, target=target)


def build(ctx: BuildContext) -> BaseChatModel:
    """deepseek-* branch: ChatAnthropic on the anthropic-compatible
    endpoint, pinned max_tokens. Reasoning effort rides
    `output_config.effort` via extra_body; `none` maps onto thinking
    disabled (the endpoint's effort vocabulary has no off level).
    """
    from shared.lm._anthropic_compat import ThinkingTokensChatAnthropic

    # Use DeepSeek's anthropic-compatible endpoint via ChatAnthropic client.
    # Not langchain-deepseek (1.0.1 reasoning_content roundtrip bug).
    # api_key comes from DEEPSEEK_API_KEY; if missing, RuntimeError fail-fast
    # immediately — don't silently use the wrong key and only find out from a
    # server 401.
    api_key = require_key("DEEPSEEK_API_KEY")

    extra_kwargs: dict[str, Any] = {}
    if ctx.thinking is not None:
        extra_kwargs["thinking"] = ctx.thinking
    if ctx.disable_streaming:
        extra_kwargs["disable_streaming"] = True

    # Per-model output cap (ModelSpec.max_output_tokens). An unregistered
    # deepseek model fails fast here rather than borrowing a wrong cap —
    # the same fail-fast posture as the unknown-prefix raise at the end.
    if ctx.spec is None or ctx.spec.max_output_tokens is None:
        known_models = ", ".join(
            sorted(
                model_id
                for model_id, registered_spec in MODELS.items()
                if registered_spec.provider == "deepseek"
            )
        )
        raise ValueError(
            f"Unknown deepseek model {ctx.model!r} — register it (with "
            f"max_output_tokens) in `shared/lm/registry.py:MODELS`. "
            f"Known deepseek models: {known_models}"
        )
    max_tokens = ctx.spec.max_output_tokens

    # Reasoning effort: DeepSeek's anthropic-compat endpoint accepts
    # `output_config.effort` to control reasoning depth (the official
    # benchmark 80.6% Verified runs the max mode). Note: the standard
    # anthropic `budget_tokens` is *ignored* on this endpoint (DeepSeek
    # docs explicitly say so); must go through output_config.
    # Passed via model_kwargs.extra_body to the underlying Anthropic SDK,
    # which merges extra_body into the POST body without touching schema.
    #
    # thinking={"type":"disabled"} is mutually exclusive with reasoning_effort
    # (server 400: "thinking options type cannot be disabled when
    # reasoning_effort is set"). Caller explicitly disabling thinking →
    # also skip effort injection; don't let global env sneakily push
    # reasoning back (the labeler short-text path is exactly this case).
    #
    # The effort goes through the same per-model clamp as claude
    # (`ModelSpec.effort_levels`), and "none" — the only cross-provider
    # value that means off rather than a level — lands on the endpoint's
    # own off-switch, `thinking={"type":"disabled"}` (the mimo branch makes
    # the same mapping). DeepSeek's vocabulary has no `none` variant, so
    # sending it unclamped 400s the request: that is what took every
    # `ava.web.fetch` down, since AVA_WEB_FETCH_REASONING ships as "none".
    deepseek_model_kwargs: dict[str, Any] = {}
    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"
    effort = ctx.resolved_effort
    if effort and not thinking_disabled:
        wire_effort = deepseek_wire_effort(
            effort,
            (ctx.effort_levels if ctx.effort_levels is not None else _DEEPSEEK_EFFORT_LEVELS),
            target=ctx.model,
        )
        if wire_effort is not None:
            deepseek_model_kwargs["extra_body"] = {"output_config": {"effort": wire_effort}}
        elif ctx.thinking is None:
            # A caller-passed `thinking` states its own intent and wins.
            extra_kwargs["thinking"] = {"type": "disabled"}

    # type ignore same as claude branch: langchain-anthropic stub treats
    # model / api_key / base_url / max_tokens as pydantic alias; signature
    # only exposes canonical field names model_name / anthropic_api_key /
    # anthropic_api_url / max_tokens_to_sample; runtime goes through
    # alias. Each parameter line of a multi-line call needs its own
    # ignore — pyright does not propagate from the opening paren line to
    # subsequent keyword argument lines.
    return ThinkingTokensChatAnthropic(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,
        base_url=_DEEPSEEK_ANTHROPIC_BASE_URL,
        max_tokens=max_tokens,  # type: ignore[call-arg]
        model_kwargs=deepseek_model_kwargs,
        timeout=ctx.timeout,
        **extra_kwargs,
    )


register(
    ProviderBinding(
        prefix="deepseek-",
        display_name="DeepSeek",
        key_env="DEEPSEEK_API_KEY",
        build=build,
        effort_levels=_DEEPSEEK_EFFORT_LEVELS,
        vision=False,
        anthropic_protocol=True,
        attach=AttachPolicy(
            file_size_limits={"image": 32 * 1024 * 1024},
            image_dimension_tiers=((1, 8192), (15, 4096)),
        ),
        stop_spec=None,
    ),
    models={
        # The provider's current canonical flash-tier name (user report
        # 2026-09-17, task #3750: DeepSeek retired the V4 Flash name — the
        # pricing page's footnote (1) now reads "Use `deepseek-flash` as the
        # model name"). Same window, output cap, cutoff, effort vocabulary,
        # tuning and price as the retired V4 Flash model.
        "deepseek-flash": ModelSpec(
            provider="deepseek",
            spawnable=True,
            context_window=1_000_000,
            max_output_tokens=384_000,
            knowledge_cutoff="2026-04",
            model_identity="You are running on DeepSeek Flash.",
            effort_levels=("high", "max"),
            tuning=ModelTuning(
                reasoning_effort="max",  # same as the retired v4 entries: Ava is not an auto-promoted harness
                # Stall-wave tightening (2026-09-18, task #3884): the 600 that
                # matched DeepSeek's documented up-to-10-minute queue was
                # exactly the failure — in the 09-14/15 waves a request not
                # served within minutes was not served inside 600s either (the
                # queue served ONLY keep-alive comments), and each stall
                # burned 600s (stream) + 600s (non-streaming fallback) per
                # turn before crashing. 150 keeps generous queue headroom
                # while cutting stall detection ~4x; the non-streaming
                # fallback segment runs under this same bound.
                llm_stream_ttft_timeout_seconds=150.0,
                # Same decision as every deepseek entry (2026-08-29): soft 374k /
                # hard 512k on the 1M window — 0.512 / 0.374 exactly.
                auto_compact_fraction=0.512,
                compact_reminder_fraction=0.374,
            ),
        ),
        # Removed 2026-09-23: deepseek-v4-pro, deepseek-v4-flash, and
        # deepseek-v4-flash-vision-exp cannot be selected. Their complete price
        # histories remain in shared/lm/pricing_catalog_archive.json; configs
        # naming them fail validation as unknown models.
        # deepseek-v4.1-flash-expires-on-0910 (internal beta, announced 2026-09-08,
        # expired 2026-09-10) was removed from the registry on 2026-09-11: it is not
        # callable anymore, no live config pinned it (cluster sweep at removal), and a
        # tombstone would keep a zombie entry plus hand-pinned prices for a model no
        # request can serve. Its price ledger entry stays in
        # shared/lm/pricing_catalog_archive.json; a stale config naming it now fails
        # spawn validation (unknown model) instead of silently running something else.
    },
    pricing={
        # New id, new ledger row: a single open period carrying the flash
        # column's current rates (checked against the official page
        # 2026-09-17) — usage under this id can only begin with the rename,
        # and the pre-rename rate history stays in the retired
        # `deepseek-v4-flash` entry it succeeds (same single-current-period
        # shape as deepseek-v4.1-flash-expires-on-0910).
        "deepseek-flash": PriceRates(
            cache_miss=0.15,
            cache_hit=0.003,
            output=0.6,
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at="2026-09-17",
            vendor="deepseek",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="0.15",
                            cache_hit="0.003",
                            output="0.6",
                            windows=(
                                PriceWindow(
                                    start="01:00:00",
                                    end="04:00:00",
                                    cache_miss="0.3",
                                    cache_hit="0.006",
                                    output="1.2",
                                ),
                                PriceWindow(
                                    start="06:00:00",
                                    end="10:00:00",
                                    cache_miss="0.3",
                                    cache_hit="0.006",
                                    output="1.2",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    },
)
