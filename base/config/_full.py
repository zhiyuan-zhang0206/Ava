"""The eager config assembly — everything `import base.config` used to do.

Imported only by `base/config/_lite.py:upgrade()`, on the first touch of a
config surface outside the boot-lite index: the sixteen per-domain sub-model
imports, the flat field registry walk, the `Settings` aggregate, and the
eager-only re-exports (the metadata / service-read / turn-view helpers) the
facade publishes after the switch. `build()` constructs the singleton from the
prepared environment and returns it together with those exports; the boot-lite
runtime installs both and replays any pending overlay writes.

`Settings.__module__` is re-pinned to "base.config": the class MOVED here,
but its public identity (repr, pickle paths) must not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, Field

import base.host.env.config_registry as _config_registry
from base.config.domains.agent.settings import AgentSettings
from base.config.domains.channels.feishu import FeishuSettings
from base.config.domains.channels.telegram import TelegramSettings
from base.config.domains.daemon.settings import DaemonSettings
from base.config.domains.display import DisplaySettings
from base.config.domains.gateway import GatewaySettings
from base.config.domains.general import GeneralSettings
from base.config.domains.lm import LmSettings
from base.config.domains.observability.alerts import AlertsSettings
from base.config.domains.observability.settings import ObservabilitySettings
from base.config.domains.packages import PackagesSettings
from base.config.domains.sandbox import SandboxSettings
from base.config.domains.services.settings import ServiceSettings
from base.config.domains.storage.data_plane import DataPlaneSettings
from base.config.domains.storage.walg import WalgSettings
from base.config.domains.web import WebSettings
from base.config.profiles import (
    PROCESS_PROFILES,
    PROFILE_UNSET,
    ProcessProfile,
    profile_domain_error,
    profile_unknown_error,
)
from base.host.env.config_registry import DOMAIN_ATTRS, DOMAIN_MODELS, schema_extra
from base.host.env.dotenv_boot import EnvBootResult


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
    # Runtime delivery facts belong to this config build, not to an env alias
    # or the public config packet. DB handles copy them with their URL slice.
    env_boot: EnvBootResult = Field(default_factory=EnvBootResult, exclude=True, repr=False)

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
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    packages: PackagesSettings = Field(default_factory=PackagesSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    walg: WalgSettings = Field(default_factory=WalgSettings)
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
            for attr, *_rest in DOMAIN_MODELS:
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
            and name in DOMAIN_ATTRS
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
# identity must not: repr / pickle paths keep reading base.config.Settings.
Settings.__module__ = "base.config"


@dataclass(frozen=True)
class FullBundle:
    """What an upgrade installs: the constructed singleton plus every name the
    facade publishes only in full mode (Settings, _FIELDS, the re-exports)."""

    settings: Any
    exports: dict[str, Any]


def build(
    *,
    env_boot: EnvBootResult,
    environment: dict[str, str] | None = None,
    profile: ProcessProfile | None = None,
) -> FullBundle:
    """Construct the eager chain from the prepared environment.

    The environment work — the `.env` load, the config-source decision, the
    cluster clock — already ran in `_lite.prepare()`, in the same order the
    eager boot used; construction is the only step left here."""
    if environment is None:
        settings = Settings(env_boot=env_boot)
    else:
        from base.config.service_read import domain_model_classes

        domains = {
            name: model.from_environment(environment)
            for name, model in domain_model_classes().items()
        }
        settings = Settings(profile=profile, env_boot=env_boot, **domains)
    return FullBundle(settings=settings, exports=_facade_exports())


def _facade_exports() -> dict[str, Any]:
    """The names `base.config` gains when a lite process upgrades."""
    from base.config.admin.metadata import (
        CONFIG_UNCHANGED_SENTINEL,
        ConfigFieldMeta,
        env_override_values,
        get_config_metadata,
    )
    from base.config.agent_pins import resolve_agent_config_pins
    from base.config.service_read import bootstrap_config_values, current_field_values

    fields = _config_registry.fields()
    return {
        "Settings": Settings,
        "_FIELDS": fields,
        "FIELD_INFOS": {name: ref.info for name, ref in fields.items()},
        "BOOTSTRAP_FIELDS": _bootstrap_fields(),
        "flat_dump": flat_dump,
        "get_config_metadata": get_config_metadata,
        "env_override_values": env_override_values,
        "ConfigFieldMeta": ConfigFieldMeta,
        "CONFIG_UNCHANGED_SENTINEL": CONFIG_UNCHANGED_SENTINEL,
        "current_field_values": current_field_values,
        "bootstrap_config_values": bootstrap_config_values,
        "field_lifecycle": _config_registry.field_lifecycle,
        "frozen_field_names": _config_registry.frozen_field_names,
        "live_field_names": _config_registry.live_field_names,
        "resolve_agent_config_pins": resolve_agent_config_pins,
        # The sub-model classes the facade used to import (tests and the
        # config-service read paths reach them through base.config).
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
        "SandboxSettings": SandboxSettings,
        "ServiceSettings": ServiceSettings,
        "TelegramSettings": TelegramSettings,
        "WalgSettings": WalgSettings,
        "WebSettings": WebSettings,
    }


def _bootstrap_fields() -> tuple[str, ...]:
    """Cluster-common config an agent-runner fetches from the gateway via
    GET /api/bootstrap. Derived from each field's ownership scope: the two
    cluster scopes are distributed; host / agent fields are not."""
    return tuple(
        name
        for name, ref in _config_registry.fields().items()
        if schema_extra(ref.info).get("scope") in ("cluster-pinned", "cluster-default")
        and schema_extra(ref.info).get("bootstrap", True) is not False
    )


def flat_dump(authority: Any, mode: str = "python") -> dict[str, Any]:
    """Flat boot values from an explicitly supplied configuration authority."""
    return authority.flat_dump(mode=mode)


def refresh_data_plane_settings(settings: Any) -> None:
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
    data-plane sub-model in place, so existing `from base.config import
    settings` references see the fresh credentials without a process restart.
    Other domains are left untouched: the rotation is a data-plane fact, and a
    full singleton swap would surprise subsystems that cache a sub-model.
    """
    from base.host.env.dotenv_boot import load_ava_env

    result = load_ava_env()
    data_plane = DataPlaneSettings()  # pyright: ignore[reportCallIssue]
    settings.data_plane = data_plane
    settings.env_boot = result
