# meiva:ref meiva_builtins/plugins/docs/plugins.meiva.okf.md#lm-deepseek-provider
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

from base.lm.effort import validate_effort
from base.lm.provider_api import (
    AttachPolicy,
    BuildContext,
    PricePeriod,
    PriceRates,
    PriceTier,
    PriceWindow,
    ProviderBinding,
    ProviderContribution,
    require_key,
)
from base.lm.registry import ModelSpec, ModelTuning
from base.packages.plugins.extensions import PluginContributions

_DEEPSEEK_EFFORT_LEVELS = ("high", "max")


def deepseek_wire_effort(effort: str, levels: tuple[str, ...], *, target: str) -> str | None:
    if effort == "none":
        return None
    return validate_effort(effort, levels, target=target)


def _require_deepseek_key() -> str:
    try:
        return require_key("DEEPSEEK_API_KEY")
    except RuntimeError:
        from base.config import field_alias
        from base.host.env.dotenv_file import read_env_aliases

        key = read_env_aliases().get(field_alias("deepseek_api_key"))
        if key:
            return key
        raise


def build(ctx: BuildContext) -> BaseChatModel:
    from meiva_builtins.plugins.lm_deepseek.chat import (
        DEEPSEEK_ANTHROPIC_API_BASE,
        DeepSeekChatModel,
    )

    api_key = _require_deepseek_key()

    extra_kwargs: dict[str, Any] = {}
    if ctx.thinking is not None:
        extra_kwargs["thinking"] = ctx.thinking
    if ctx.disable_streaming:
        extra_kwargs["disable_streaming"] = True

    if ctx.spec is None or ctx.spec.max_output_tokens is None:
        raise ValueError(
            f"Unknown deepseek model {ctx.model!r} — register it (with "
            "max_output_tokens) in this plugin's ModelSpec rows. "
            f"Known deepseek models: {', '.join(ctx.provider_model_ids)}"
        )
    max_tokens = ctx.spec.max_output_tokens

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
            extra_kwargs["thinking"] = {"type": "disabled"}

    model_cls: Any = DeepSeekChatModel
    return model_cls(
        model=ctx.model,
        api_key=api_key,
        base_url=DEEPSEEK_ANTHROPIC_API_BASE,
        max_tokens=max_tokens,
        model_kwargs=deepseek_model_kwargs,
        timeout=ctx.timeout,
        **extra_kwargs,
    )


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
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
        "deepseek-flash": ModelSpec(
            provider="deepseek",
            spawnable=True,
            context_window=1_000_000,
            max_output_tokens=384_000,
            knowledge_cutoff="2026-04",
            model_identity="You are running on DeepSeek Flash.",
            effort_levels=("high", "max"),
            media_types=frozenset({"image"}),
            tuning=ModelTuning(
                reasoning_effort="max",
                llm_stream_ttft_timeout_seconds=150.0,
                auto_compact_fraction=0.512,
                compact_reminder_fraction=0.374,
            ),
        ),
    },
    pricing={
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


def contribute() -> PluginContributions:
    return PluginContributions(providers=(PROVIDER,))
