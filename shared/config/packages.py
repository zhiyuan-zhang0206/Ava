"""Packages config — PackagesSettings.

The per-machine content-refresh policy plane (tasks #2915 / #3267): the
feature's master switch, the defaults resolved into registry rows at first
sight, the OS job's base tick, and the executor's per-run bounds. Per-package
overrides live in the registry (`UpdateState`), not here — these values are
the cluster-configured floor every unresolved row falls back to.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from shared.config._base import EnvSettings

UpdateModeSetting = Literal["auto", "notify", "off"]

# The json_schema_extra metadata every field in this domain carries: host-
# scoped (per-machine), operator-writable, nothing to restart. `Any` because
# pydantic types json_schema_extra as dict[str, JsonValue] and a shared
# constant cannot infer its way into that JsonValue union.
_HOST_FIELD: Any = {
    "restart_required": "",
    "writable": True,
    "sensitive": False,
    "scope": "host",
    "remote_writable": False,
}


class PackagesSettings(EnvSettings):
    refresh_enabled: bool = Field(
        default=True,
        alias="AVA_PACKAGES_REFRESH_ENABLED",
        description="Master switch for the content-refresh channel (core skills fast lane): the converge step registers the 15-min OS job only when on, and a job run no-ops when off. Manual `ava packages refresh` always works.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_default_mode: UpdateModeSetting = Field(
        default="auto",
        alias="AVA_PACKAGES_REFRESH_DEFAULT_MODE",
        description="Update policy a channel-backed package resolves to at first sight when its registry row carries no explicit mode: auto (check + apply), notify (check, record, never apply), off.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_default_interval_seconds: int = Field(
        default=86400,
        gt=0,
        alias="AVA_PACKAGES_REFRESH_DEFAULT_INTERVAL_SECONDS",
        description="Check interval a channel-backed package resolves to at first sight when its row carries none. 24h is the user's standing ruling (2026-09-11): one cadence for every channel-backed package.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_tick_seconds: int = Field(
        default=900,
        gt=0,
        alias="AVA_PACKAGES_REFRESH_TICK_SECONDS",
        description="Base tick of the per-machine refresh OS job (seconds). Per-package intervals are registry data, so the tick only bounds granularity — one job, no re-registration when a policy changes.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_budget_seconds: float = Field(
        default=120.0,
        gt=0,
        alias="AVA_PACKAGES_REFRESH_BUDGET_SECONDS",
        description="Wall-clock budget for one refresh pass (seconds). The pass stops taking new work when it is spent and records what it skipped; a later pass picks the rest up.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_max_applies: int = Field(
        default=10,
        gt=0,
        alias="AVA_PACKAGES_REFRESH_MAX_APPLIES",
        description="Upper bound on staged applies in one refresh pass, so a large backlog cannot land all at once.",
        json_schema_extra=_HOST_FIELD,
    )

    refresh_network_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        alias="AVA_PACKAGES_REFRESH_NETWORK_TIMEOUT_SECONDS",
        description="Timeout for one git network operation (ls-remote / fetch) during refresh (seconds). A timeout is a recorded error + backoff, never a retry loop.",
        json_schema_extra=_HOST_FIELD,
    )
