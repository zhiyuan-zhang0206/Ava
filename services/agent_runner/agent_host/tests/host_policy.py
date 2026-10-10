"""Existing host tests explicitly assemble their process-default policy inputs."""

from base.config import settings
from services.agent_runner.agent_host.runtime import HostCachePolicy, HostPolicy


def configured_policy() -> HostPolicy:
    """Keep legacy config-mutation cases at the test's composition boundary."""
    return HostPolicy(
        max_concurrent_turns=settings.daemon.host_max_concurrent_turns,
        cache=lambda: HostCachePolicy(
            idle_ttl_seconds=settings.daemon.host_agent_idle_ttl_seconds,
            size=settings.daemon.host_agent_cache_size,
        ),
        default_model=lambda: settings.lm.llm_model,
        llm_override=lambda: settings.lm.llm_override,
    )
