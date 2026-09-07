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
    # gateway's event emitter dual-writes via shared.telemetry_otlp, which
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
            # The PITR uploader daemon reads the physical-backup plane under
            # the gateway profile (bucket/key/credentials).
            "physical_backup",
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
            "gateway",
            "services",
            "daemon",
            # ops/spec.py's pitr-uploader roster gate is reachable from the
            # agent closure (via the fleet plugin); only gateway/runner
            # processes read the domain at runtime.
            "physical_backup",
        }
    ),
    # runner support daemons (ops / watchdog / browser / browser-mcp /
    # gate / permissions-helper / healthchecks). sandbox is consumed by the
    # browser MCP daemon (settings.sandbox.mcp_connect_timeout_seconds);
    # observability by the daemons' event emitter via shared.telemetry_otlp.
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
            # ops/spec.py gates the pitr-uploader roster entry on AVA_PITR_ENABLED.
            "physical_backup",
        }
    ),
}

# Sentinel distinguishing "no argument" (read AVA_PROCESS_PROFILE from the
# environment) from an explicit `profile=None` (full construction — the
# config-service read paths use this). A string no process-profile name can
# ever be, so the default also type-checks as the parameter's `str | None`.
PROFILE_UNSET = "!unset!"
