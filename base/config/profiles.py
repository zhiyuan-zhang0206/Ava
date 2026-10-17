# meiva:ref base/config/docs/process-profiles.meiva.okf.md#process-domains
from __future__ import annotations

from typing import Literal

MEIVA_PROCESS_PROFILE_ENV = "MEIVA_PROCESS_PROFILE"
ProcessProfile = Literal["gateway", "agent", "runner"]

PROCESS_PROFILES: dict[ProcessProfile, frozenset[str]] = {
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
            "display",
            "to_user",
        }
    ),
    "agent": frozenset(
        {
            "agent",
            "lm",
            "sandbox",
            "web",
            "data_plane",
            "general",
            "observability",
            "display",
            "gateway",
            "services",
            "daemon",
            "to_user",
        }
    ),
    "runner": frozenset(
        {
            "services",
            "daemon",
            "general",
            "data_plane",
            "gateway",
            "lm",
            "sandbox",
            "observability",
            "to_user",
        }
    ),
}


PROFILE_UNSET = "!unset!"


def profile_domain_error(profile: str, domain: str) -> AttributeError:
    return AttributeError(
        f"'{profile}' process profile does not construct the {domain!r} config "
        f"domain (per-process config, Task #856) — nothing in this process "
        f"kind reads settings.{domain}. If this read is legitimate, add the "
        f"domain to the '{profile}' profile in PROCESS_PROFILES AND to the "
        f"consumption-matrix guard (cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py); "
        f"otherwise move the read to the process kind that owns the domain. "
        f"Dynamic code can check settings.has_domain({domain!r}) first."
    )


def profile_unknown_error(profile: str) -> ValueError:
    return ValueError(
        f"{MEIVA_PROCESS_PROFILE_ENV}={profile!r} is not a known process profile; "
        f"must be one of {sorted(PROCESS_PROFILES)} — the marker is set by the "
        f"process launcher, not by hand"
    )
