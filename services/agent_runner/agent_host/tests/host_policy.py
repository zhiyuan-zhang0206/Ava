"""Existing host tests explicitly assemble their process-default policy inputs."""

from base.agents.impersonation.notes import HandoffNotes
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import settings
from base.config.tests.configuration import snapshot_process_config
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
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        clock_factory=Clock.from_settings,
        handoff_notes=HandoffNotes(
            clock_factory=Clock.from_settings,
            timestamps_enabled=lambda: settings.general.message_timestamps,
        ),
    )


def configured_machine(*, name: str | None = None) -> MachineIdentity:
    """Assemble a test host's machine from its current prepared configuration."""
    machine = machine_from_boot(snapshot_process_config())
    if name is not None:
        machine.set(name=name)
    return machine


def assert_sdk_context(expected: object | None, *, bound: bool) -> None:
    """Observe the existing SDK slot without resolving machine identity."""
    import ava

    assert getattr(ava, "context", None) is expected
    assert hasattr(ava, "context") is bound
