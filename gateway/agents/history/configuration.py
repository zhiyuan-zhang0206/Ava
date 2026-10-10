"""Compose history readers from the gateway's actual configuration authority."""

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock, clock_config_from_boot
from base.config import ConfigBoot
from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.config.service_read import ConfigAuthority
from gateway.agents.history.read_inputs import TimelineReadPolicy, UnderstandingReadPolicy

__all__ = ["timeline_policy", "understanding_policy"]


def timeline_policy(config: ConfigBoot) -> TimelineReadPolicy:
    """Capture an owner without reading policy until the request needs it."""
    return TimelineReadPolicy(
        rendering=TimelineReadInputs(
            clock_factory=lambda: Clock(clock_config_from_boot(config)),
            timestamps_enabled=lambda: config.view.general.message_timestamps,
        ),
        default_limit=lambda: config.view.display.timeline_default_limit,
        compact_history=lambda: config.view.gateway.timeline_compact_history,
    )


def _chunk_ratio(config: ConfigBoot, authority: ConfigAuthority) -> float:
    if config.view.has_domain("agent"):
        return config.view.agent.understanding_chunk_ratio
    raw = authority.read_env_aliases().get("AVA_UNDERSTANDING_CHUNK_RATIO")
    if raw:
        return float(raw)
    return AgentRuntimeSettings.model_fields["understanding_chunk_ratio"].default


def _enabled(config: ConfigBoot, authority: ConfigAuthority) -> bool:
    if config.view.has_domain("agent"):
        return config.view.agent.understanding_enabled
    raw = authority.read_env_aliases().get("AVA_UNDERSTANDING_ENABLED")
    if raw is None:
        return AgentRuntimeSettings.model_fields["understanding_enabled"].default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def understanding_policy(config: ConfigBoot, authority: ConfigAuthority) -> UnderstandingReadPolicy:
    """Keep gateway-profile file reads live without constructing the agent domain."""
    return UnderstandingReadPolicy(
        rendering=timeline_policy(config).rendering,
        hierarchy_model=lambda: config.view.lm.hierarchy_model,
        default_model=lambda: config.view.lm.llm_model,
        chunk_ratio=lambda: _chunk_ratio(config, authority),
        enabled=lambda: _enabled(config, authority),
    )
