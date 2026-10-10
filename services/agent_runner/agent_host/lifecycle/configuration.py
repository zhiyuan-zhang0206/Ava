"""Assemble one host root's live policy inputs from its configuration owner."""

from collections.abc import Callable
from typing import Any

from ava.sdk_surface.install import Installation
from base import paths
from base.agents.context.clients import ClientSet
from base.agents.history.hierarchy.group_consumer import UnderstandingReadInputs
from base.agents.history.inbound_sideload import ReconcileReadInputs
from base.agents.impersonation.notes import HandoffNotes
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity, roles_from_flags
from base.config import ConfigBoot
from base.log import init_gateway_process
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_host.runtime import HostCachePolicy, HostPolicy

__all__ = [
    "host_machine",
    "host_policy",
    "initialize_logging",
    "load_installation",
    "understanding_inputs",
]


def host_policy(config: ConfigBoot) -> HostPolicy:
    """Keep root-time admission and operation-time readers at their original points."""
    return HostPolicy(
        max_concurrent_turns=config.view.daemon.host_max_concurrent_turns,
        cache=lambda: HostCachePolicy(
            idle_ttl_seconds=config.view.daemon.host_agent_idle_ttl_seconds,
            size=config.view.daemon.host_agent_cache_size,
        ),
        default_model=lambda: config.view.lm.llm_model,
        llm_override=lambda: config.view.lm.llm_override,
        default_reader=lambda domain, field: getattr(getattr(config.view, domain), field),
        clock_factory=lambda: Clock(clock_config_from_boot(config)),
        handoff_notes=HandoffNotes(
            clock_factory=lambda: Clock(clock_config_from_boot(config)),
            timestamps_enabled=lambda: config.view.general.message_timestamps,
        ),
        recovery_wake_enabled=lambda: config.view.daemon.hosted_crash_recovery_wake_enabled,
        recrash_reap_enabled=lambda: config.view.daemon.hosted_recrash_prompt_reap_enabled,
        reconcile_inputs=ReconcileReadInputs(
            stale_claimed_seconds=lambda: (
                config.view.daemon.delivery_watchdog_stale_claimed_threshold_seconds
            ),
            clock_pad_seconds=lambda: config.view.daemon.inbound_reconcile_clock_pad_seconds,
            boundary_scan_limit=lambda: config.view.daemon.inbound_reconcile_boundary_scan_limit,
            window_row_cap=lambda: config.view.daemon.inbound_reconcile_window_row_cap,
        ),
    )


def understanding_inputs(config: ConfigBoot) -> UnderstandingReadInputs:
    """Compose the existing hierarchy consumer readers without reading any value."""
    return UnderstandingReadInputs(
        enabled=lambda: config.view.agent.understanding_enabled,
        default_model=lambda: config.view.lm.llm_model,
        hierarchy_model=lambda: config.view.lm.hierarchy_model,
        group_model=lambda: config.view.agent.understanding_group_model,
        check_open=lambda: config.view.agent.understanding_group_check_open,
        check_decay=lambda: config.view.agent.understanding_group_check_decay,
        reasoning=lambda: config.view.agent.understanding_group_reasoning,
        corrections=lambda: config.view.agent.understanding_group_corrections,
        clock_factory=lambda: Clock(clock_config_from_boot(config)),
        timestamps_enabled=lambda: config.view.general.message_timestamps,
    )


def host_machine(config: ConfigBoot) -> MachineIdentity:
    """Keep machine validation lazy on the same process configuration owner."""
    return MachineIdentity(
        name=lambda: config.view.general.machine_name,
        role=lambda: roles_from_flags(
            serve_gateway=config.view.general.machine_serve_gateway,
            serve_agent_runner=config.view.general.machine_serve_agent_runner,
            serve_observability_station=config.view.general.machine_serve_observability_station,
        ),
        host=lambda: config.view.general.machine_host,
        description=lambda: config.view.general.machine_description,
    )


def load_installation(
    config: ConfigBoot,
    *,
    producer: Callable[[], Any],
    load_extensions: Callable[..., object],
) -> Installation:
    """Load one installation: its registry feeds the graph and its configs feed each turn.

    The shared graph is built once and cannot take a new registry. The host
    retains this installation's boot image until its normal process restart.
    """
    from ava.sdk_surface.install import installed
    from base.config import Settings
    from base.config.service_read import ConfigAuthority
    from base.lm.plugin_providers import build_model_catalog

    env_path = paths.ava_home() / ".env"
    if config.view.profile is None:
        authority = ConfigAuthority(runtime=config.view, all_domains=config.view, env_path=env_path)
    else:
        authority = ConfigAuthority.deferred(
            runtime=config.view, build_all_domains=lambda: Settings(profile=None), env_path=env_path
        )
    load_extensions(
        catalog=build_model_catalog(),
        authority=authority,
        producer=producer,
        clock_factory=lambda: Clock(clock_config_from_boot(config)),
    )
    installation = installed()
    if installation is None:
        raise RuntimeError("the plugin load did not install its SDK surface")
    return installation


def initialize_logging(clients: ClientSet, machine: MachineIdentity, image: LoadedCommit) -> None:
    try:
        init_gateway_process(
            name="agent_host",
            producer=clients.event_pipeline,
            machine_reader=machine.name,
            image=image,
        )
    except BaseException as primary:
        try:
            clients.close()
        except BaseException as secondary:
            primary.add_note(f"Host logging cleanup also failed: {secondary!r}")
        raise
