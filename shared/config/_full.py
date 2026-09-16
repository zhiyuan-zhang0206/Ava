"""The eager config assembly — everything `import shared.config` used to do.

Imported only by `shared/config/_lite.py:upgrade()`, on the first touch of a
config surface outside the boot-lite index: the fifteen per-domain sub-model
imports, the flat field registry walk, the `Settings` aggregate, and the
eager-only re-exports (the metadata / service-read / turn-view helpers) the
facade publishes after the switch. `build()` constructs the singleton from the
prepared environment and returns it together with those exports; the boot-lite
runtime installs both and replays any pending overlay writes.

`Settings.__module__` is re-pinned to "shared.config": the class MOVED here,
but its public identity (repr, pickle paths) must not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, Field

import shared.config_registry as _config_registry
from shared.config._lite import _plant_placeholders
from shared.config.agent import AgentSettings
from shared.config.alerts import AlertsSettings
from shared.config.daemon import DaemonSettings
from shared.config.data_plane import DataPlaneSettings
from shared.config.data_plane import (
    _self_machine_host as _self_machine_host,  # re-export: service_read resolves it through shared.config so tests can monkeypatch it
)
from shared.config.display import DisplaySettings
from shared.config.feishu import FeishuSettings
from shared.config.gateway import GatewaySettings
from shared.config.general import GeneralSettings
from shared.config.lm import LmSettings
from shared.config.observability import ObservabilitySettings
from shared.config.packages import PackagesSettings
from shared.config.physical_backup import PhysicalBackupSettings
from shared.config.profiles import (
    PROCESS_PROFILES,
    PROFILE_UNSET,
    ProcessProfile,
    profile_domain_error,
    profile_unknown_error,
)
from shared.config.sandbox import SandboxSettings
from shared.config.services import ServiceSettings
from shared.config.telegram import TelegramSettings
from shared.config.web import WebSettings
from shared.config_registry import _DOMAIN_ATTRS, _DOMAIN_MODELS, _schema_extra


class Settings(BaseModel):
    """Aggregate of the per-domain config sub-models. Access is nested:
    `settings.lm.llm_model`, `settings.data_plane.db_url`. Each sub-model is a
    `BaseSettings` that reads the flat env; this composite just holds one of each.

    `profile` selects the per-process domain set (PROCESS_PROFILES): a domain
    outside the profile is NOT constructed and its attribute access raises an
    actionable AttributeError (fail-fast — a cross-profile read used to
    silently read a default). `has_domain()` is the dynamic-code escape hatch.
    With no profile marker the composite constructs every domain, unchanged.
    """

    # The process profile this aggregate was constructed for (None = full
    # construction). A plain excluded field, not a PrivateAttr: the profile
    # fail-fast in __getattr__ reads it as a normal attribute, and
    # model_dump()/validation never sees it (exclude=True).
    profile: str | None = Field(default=None, exclude=True)

    lm: LmSettings = Field(default_factory=LmSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    # DataPlaneSettings has required no-default fields (db_url / redis_url) that
    # BaseSettings fills from env at construction; pyright sees the zero-arg factory
    # as under-supplied.
    data_plane: DataPlaneSettings = Field(default_factory=DataPlaneSettings)  # pyright: ignore[reportArgumentType, reportUnknownVariableType]
    physical_backup: PhysicalBackupSettings = Field(default_factory=PhysicalBackupSettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    packages: PackagesSettings = Field(default_factory=PackagesSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)

    def __init__(self, *, profile: str | None = PROFILE_UNSET, **data: Any) -> None:
        """Construct the aggregate for `profile`.

        `profile` defaults to the process's AVA_PROCESS_PROFILE env marker; an
        explicit `profile=None` builds every domain (config-service read paths,
        tests, CLI). An unknown profile name fails fast — the marker is set by
        launchers, not by hand.
        """
        if profile == PROFILE_UNSET:
            profile = os.environ.get("AVA_PROCESS_PROFILE")
        if profile is not None and profile not in PROCESS_PROFILES:
            raise profile_unknown_error(profile)
        super().__init__(**data)
        self.profile = profile
        if profile is not None:
            allowed = PROCESS_PROFILES[profile]
            for attr, *_rest in _DOMAIN_MODELS:
                if attr not in allowed:
                    vars(self).pop(attr, None)

    def __getattr__(self, name: str) -> Any:
        # A missing attribute on this aggregate is either a typo or — on a
        # profile-limited instance — a domain the process profile deliberately
        # does not construct (fail-fast: a cross-profile read used to silently
        # read a default). The actionable message names the fix for both.
        profile = self.profile
        if (
            profile is not None
            and name in _DOMAIN_ATTRS
            and name not in PROCESS_PROFILES[cast_profile(profile)]
        ):
            raise profile_domain_error(profile, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    def has_domain(self, name: str) -> bool:
        """Whether this process's profile constructs the `name` config domain.

        The escape hatch for dynamic code (plugins) that must probe before
        reading; static code should simply access `settings.<domain>` and let
        the fail-fast AttributeError point at the fix.
        """
        profile = self.profile
        if profile is None:
            return True
        return name in PROCESS_PROFILES[cast_profile(profile)]


def cast_profile(profile: str) -> ProcessProfile:
    """Narrow a validated profile name for the PROCESS_PROFILES lookup (the
    name was checked against the dict at construction)."""
    return cast("ProcessProfile", profile)


# The class MOVED into this module (the boot-lite split), but its public
# identity must not: repr / pickle paths keep reading shared.config.Settings.
Settings.__module__ = "shared.config"


@dataclass(frozen=True)
class FullBundle:
    """What an upgrade installs: the constructed singleton plus every name the
    facade publishes only in full mode (Settings, _FIELDS, the re-exports)."""

    settings: Any
    exports: dict[str, Any]


def build() -> FullBundle:
    """Construct the eager chain from the prepared environment.

    The environment work — the `.env` load, the config-source decision, the
    cluster clock — already ran in `_lite.prepare()`, in the same order the
    eager boot used; construction is the only step left here."""
    return FullBundle(settings=Settings(), exports=_facade_exports())


def _facade_exports() -> dict[str, Any]:
    """The names `shared.config` gains when a lite process upgrades."""
    from shared.config import metadata as _metadata
    from shared.config import service_read as _service_read
    from shared.config import turn_view as _turn_view

    fields = _config_registry._fields()
    return {
        "Settings": Settings,
        "_FIELDS": fields,
        "FIELD_INFOS": {name: ref.info for name, ref in fields.items()},
        "BOOTSTRAP_FIELDS": _bootstrap_fields(),
        "flat_dump": flat_dump,
        "refresh_data_plane_settings": refresh_data_plane_settings,
        # The legacy facade-private helper (task #3621 R-1): kept resolvable
        # through the latch; its lite-era home is shared.config._lite.
        "_plant_lite_placeholders": _plant_placeholders,
        "_self_machine_host": _self_machine_host,
        "warn_deprecated_env_aliases": _service_read.warn_deprecated_env_aliases,
        "get_config_metadata": _metadata.get_config_metadata,
        "env_override_values": _metadata.env_override_values,
        "ConfigFieldMeta": _metadata.ConfigFieldMeta,
        "CONFIG_UNCHANGED_SENTINEL": _metadata.CONFIG_UNCHANGED_SENTINEL,
        "current_field_values": _service_read.current_field_values,
        "bootstrap_config_values": _service_read.bootstrap_config_values,
        "field_lifecycle": _config_registry.field_lifecycle,
        "frozen_field_names": _config_registry.frozen_field_names,
        "live_field_names": _config_registry.live_field_names,
        "resolve_agent_config_pins": _turn_view.resolve_agent_config_pins,
        # The sub-model classes the facade used to import (tests and the
        # config-service read paths reach them through shared.config).
        "AgentSettings": AgentSettings,
        "AlertsSettings": AlertsSettings,
        "DaemonSettings": DaemonSettings,
        "DataPlaneSettings": DataPlaneSettings,
        "FeishuSettings": FeishuSettings,
        "GatewaySettings": GatewaySettings,
        "GeneralSettings": GeneralSettings,
        "LmSettings": LmSettings,
        "ObservabilitySettings": ObservabilitySettings,
        "PackagesSettings": PackagesSettings,
        "PhysicalBackupSettings": PhysicalBackupSettings,
        "SandboxSettings": SandboxSettings,
        "ServiceSettings": ServiceSettings,
        "TelegramSettings": TelegramSettings,
        "WebSettings": WebSettings,
    }


def _bootstrap_fields() -> tuple[str, ...]:
    """Cluster-common config an agent-runner fetches from the gateway via
    GET /api/bootstrap. Derived from each field's ownership scope: the two
    cluster scopes are distributed; host / agent fields are not."""
    return tuple(
        name
        for name, ref in _config_registry._fields().items()
        if _schema_extra(ref.info).get("scope") in ("cluster-pinned", "cluster-default")
        and _schema_extra(ref.info).get("bootstrap", True) is not False
    )


def flat_dump(mode: str = "python") -> dict[str, Any]:
    """Flat `{field name: value}` dump across all sub-models — the shape the old
    flat `settings.model_dump()` produced, used by the config-overlay snapshot.

    Reads through the profile-independent path (`_all_domains_settings` for a
    domain the running profile excludes), so an agent process — whose profile
    excludes daemon/alerts/telegram/feishu — can still snapshot the full
    overlay base at bind time."""
    from shared.config import settings
    from shared.config.service_read import _all_domains_settings

    out: dict[str, Any] = {}
    for attr, _label, _model, _cap in _DOMAIN_MODELS:
        try:
            sub = getattr(settings, attr)
        except AttributeError:
            sub = getattr(_all_domains_settings(), attr)
        out.update(sub.model_dump(mode=mode))
    return out


def refresh_data_plane_settings() -> None:
    """Re-read the unit's `.env` and rebuild `settings.data_plane` in place.

    A long-lived process (the rollout orchestrator) builds its Settings
    singleton once at startup. When the work it drives rewrites `.env` — most
    notably a data-plane credential rotation migration run by the local leg's
    child `ava start` (the 2026-08-25 secret split) — the singleton still
    carries the pre-rotation values and every later data-plane write from this
    process fails with SASL authentication. On 2026-08-25 the pin advance, the
    compensating unpause and the update-lock release all died exactly that way,
    stranding the cluster paused with a stale pin while the watchdog
    force-checked-out the stale pin underneath the landed gateway.

    This re-runs the boot env load (the cluster-env authority pass refreshes
    the cluster-scope aliases from the new file) and rebuilds only the
    data-plane sub-model in place, so existing `from shared.config import
    settings` references see the fresh credentials without a process restart.
    Other domains are left untouched: the rotation is a data-plane fact, and a
    full singleton swap would surprise subsystems that cache a sub-model.
    """
    from shared.config import settings
    from shared.dotenv_boot import load_ava_env

    load_ava_env()
    settings.data_plane = DataPlaneSettings()  # pyright: ignore[reportCallIssue]
