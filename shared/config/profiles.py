"""Per-process config profiles — which config DOMAINS each process kind constructs.

Dependency-free constants module (like `shared/env_registry.py`): the guard test
and process-side code can import the profile sets without triggering a
Settings construction. `shared/config/__init__.py` consumes this.

AVA_PROCESS_PROFILE names the process kind — gateway / agent / runner — and is
set explicitly by each launcher (Phase 1/2: the service launcher, the
watchdog respawn paths, agent_spawn_env_dict). The Settings singleton
constructs ONLY its profile's domains; a domain outside the profile raises an
actionable AttributeError on access (fail-fast, Task #856 D2) and
`settings.has_domain()` is the escape hatch for dynamic code (plugins). A
process with NO profile marker (CLI maintenance verbs, tests, bare checkouts,
the gateway-hosted schedule runner) constructs every domain exactly as before.

The sets ARE the consumption matrix — which process kinds' code actually
reads `settings.<domain>` — verified 2026-08-06 by AST scan (PR-A) and kept
honest by the bidirectional guard in tests/shared/test_gateway_consumer_guard.py
(profile domains == domains the kind's code + import closure consumes). They
are NOT the capability axis (config-panel grouping only): deriving process env
sets from capability was the 2026-08-06 #1570 P0 (im_bridge's telegram/feishu
keys dropped).
"""

from __future__ import annotations

from typing import Literal

AVA_PROCESS_PROFILE_ENV = "AVA_PROCESS_PROFILE"
ProcessProfile = Literal["gateway", "agent", "runner"]

PROCESS_PROFILES: dict[ProcessProfile, frozenset[str]] = {
    # gateway HTTP + gateway-side daemons (im_bridge / heartbeat / labeler /
    # events_maintenance / memory_indexer / milvus / delivery_watchdog).
    # lm/telegram/feishu are real gateway-side reads (routers read llm_model;
    # im_bridge reads telegram/feishu) — capability says agent-runner, the
    # consumption matrix says gateway. Consumption wins. observability: the
    # gateway's event emitter dual-writes via shared.telemetry.otlp.telemetry_otlp, which
    # reads the AVA_TELEMETRY_OTLP_* fields (2026-08-11 OTel stack).
    "gateway": frozenset(
        {
            "data_plane",
            "gateway",
            "general",
            "services",
            "daemon",
            "alerts",
            "lm",
            "telegram",
            "feishu",
            "observability",
            # Display window defaults (task #3696) — served by gateway endpoints
            # (messages / timeline / notices / shell) for unparameterized reads.
            "display",
            # The PITR uploader daemon reads the physical-backup plane under
            # the gateway profile (bucket/key/credentials).
            "physical_backup",
            # The skills router (#3267) reads install_registry.resolved_policy(),
            # which resolves per-package update defaults from settings.packages.
            "packages",
        }
    ),
    # Agent host and exec children (kernel + SDK + builtin plugins). daemon is
    # consumed by the ava_fleet plugin's in-agent task_maintenance service
    # (task_maintenance_* / task_reminder_backoff_seconds / task_escalate_n).
    "agent": frozenset(
        {
            "agent",
            "lm",
            "sandbox",
            "web",
            "data_plane",
            "general",
            "observability",
            # Display window defaults (task #3696): the agent-published timeline
            # snapshot reads display.timeline_default_limit via shared/agents/history/timeline.
            "display",
            "gateway",
            "services",
            "daemon",
            # The agent-host manifest health monitor writes pending-age,
            # slow-seal, capture-failure, and retention-loss alerts.
            "alerts",
            # ops/spec.py's pitr-uploader roster gate is reachable from the
            # agent closure (via the fleet plugin); only gateway/runner
            # processes read the domain at runtime.
            "physical_backup",
            # ava/skills.py imports shared.install_registry, whose
            # resolved_policy() resolves per-package update defaults from
            # settings.packages (#3267).
            "packages",
        }
    ),
    # runner support daemons (ops / watchdog / browser / browser-mcp /
    # gate / permissions-helper / healthchecks). sandbox is consumed by the
    # browser MCP daemon (settings.sandbox.mcp_connect_timeout_seconds);
    # observability by the daemons' event emitter via shared.telemetry.otlp.telemetry_otlp.
    "runner": frozenset(
        {
            "services",
            "daemon",
            "general",
            "data_plane",
            "gateway",
            "lm",  # ops_lifecycle reads llm_model
            "sandbox",
            "observability",
            # Runner-owned central producers and lifecycle paths share the
            # manifest alert writer with the agent-host monitor.
            "alerts",
            # The pty CLI (shared/sessions/pty/cli.py, reachable from the runner
            # closure) resolves an omitted capture window from
            # display.shell_capture_default_lines (task #3696).
            "display",
            # ops/spec.py gates the pitr-uploader roster entry on AVA_PITR_ENABLED.
            "physical_backup",
            # shared.install_registry.resolved_policy() is reachable from the
            # runner closure and resolves per-package update defaults from
            # settings.packages (#3267).
            "packages",
        }
    ),
}

# Sentinel distinguishing "no argument" (read AVA_PROCESS_PROFILE from the
# environment) from an explicit `profile=None` (full construction — the
# config-service read paths use this). A string no process-profile name can
# ever be, so the default also type-checks as the parameter's `str | None`.
PROFILE_UNSET = "!unset!"


def profile_domain_error(profile: str, domain: str) -> AttributeError:
    """The fail-fast error for touching a config domain the process profile
    does not construct.

    One constructor, so the eager aggregate (`Settings.__getattr__`), the
    boot-lite settings view, and `set_field`/`get_field` all raise the same
    actionable message (fail-fast, Task #856 D2): a cross-profile read used to
    silently read a default.
    """
    return AttributeError(
        f"'{profile}' process profile does not construct the {domain!r} config "
        f"domain (per-process config, Task #856) — nothing in this process "
        f"kind reads settings.{domain}. If this read is legitimate, add the "
        f"domain to the '{profile}' profile in PROCESS_PROFILES AND to the "
        f"consumption-matrix guard (tests/shared/test_gateway_consumer_guard.py); "
        f"otherwise move the read to the process kind that owns the domain. "
        f"Dynamic code can check settings.has_domain({domain!r}) first."
    )


def profile_unknown_error(profile: str) -> ValueError:
    """The fail-fast error for an AVA_PROCESS_PROFILE marker outside the known
    vocabulary — the marker is set by launchers, not by hand."""
    return ValueError(
        f"{AVA_PROCESS_PROFILE_ENV}={profile!r} is not a known process profile; "
        f"must be one of {sorted(PROCESS_PROFILES)} — the marker is set by the "
        f"process launcher, not by hand"
    )
