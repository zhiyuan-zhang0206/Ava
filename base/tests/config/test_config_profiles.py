from __future__ import annotations

from pathlib import Path

import pytest

from base.config.service_read import ConfigAuthority


@pytest.mark.usefixtures("served_gateway_home")
def test_service_reads_stay_full_when_singleton_domain_excluded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base import config

    real = config.settings

    class _GatewayProfileLimited:
        def has_domain(self, name: str) -> bool:
            return name not in ("agent", "sandbox", "web") and real.has_domain(name)

        def __getattr__(self, name: str):
            if name in ("agent", "sandbox", "web"):
                raise AttributeError(
                    f"gateway profile does not construct the {name} domain (Task #856)"
                )
            return getattr(real, name)

    authority = ConfigAuthority(config.settings, config.settings, tmp_path / ".env")
    baseline_bootstrap = authority.bootstrap_config_values(
        provider_key_envs=(), plugin_cluster_config=""
    )
    baseline_values = authority.current_field_values()
    baseline_flat = authority.flat_dump(mode="json")

    monkeypatch.setattr(config, "settings", _GatewayProfileLimited())

    authority = ConfigAuthority(config.settings, authority.all_domains, authority.env_path)
    served = authority.bootstrap_config_values(provider_key_envs=(), plugin_cluster_config="")
    values = authority.current_field_values()
    flat = authority.flat_dump(mode="json")

    assert served == baseline_bootstrap
    assert values == baseline_values
    assert flat == baseline_flat

    for name in ("exec_timeout_seconds", "prompt_invest_future_enabled", "web_fetch_model"):
        assert name in values, name
        assert name in flat, name
    assert "MEIVA_EXEC_TIMEOUT_SECONDS" in served


def test_get_field_stays_strict_on_excluded_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    real = config.settings

    class _GatewayProfileLimited:
        def __getattr__(self, name: str):
            if name in ("agent", "sandbox", "web"):
                raise AttributeError(f"{name} not in gateway profile (Task #856)")
            return getattr(real, name)

    monkeypatch.setattr(config, "settings", _GatewayProfileLimited())
    import pytest

    with pytest.raises(AttributeError):
        config.get_field("exec_timeout_seconds")


def _profile_settings(monkeypatch: pytest.MonkeyPatch, profile: str):
    from base import config

    return config.Settings(profile=profile)


def test_gateway_profile_excludes_agent_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    s = config.Settings(profile="gateway")
    for domain in ("sandbox", "agent", "web"):
        assert not s.has_domain(domain)
        with pytest.raises(AttributeError) as exc:
            getattr(s, domain)
        assert "gateway" in str(exc.value) and domain in str(exc.value)
        assert "has_domain" in str(exc.value)
    for domain in (
        "lm",
        "telegram",
        "feishu",
        "data_plane",
        "services",
        "daemon",
        "alerts",
        "gateway",
        "general",
    ):
        assert s.has_domain(domain), domain


def test_agent_profile_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    s = config.Settings(profile="agent")
    for domain in (
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
    ):
        assert s.has_domain(domain), domain
    for domain in ("alerts", "telegram", "feishu"):
        assert not s.has_domain(domain), domain
        with pytest.raises(AttributeError):
            getattr(s, domain)


def test_keys_of_the_retired_stack_are_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from base import config

    for key, value in {
        "MEIVA_PITR_ENABLED": "true",
        "MEIVA_PITR_STORE_BACKEND": "oss",
        "MEIVA_PITR_OSS_BUCKET": "retired",
        "MEIVA_PITR_GCS_PREFIX": "meiva-pitr/retired",
        "MEIVA_PITR_SPOOL_HARD_BYTES": "2362232013",
        "MEIVA_PITR_UPLOADER_HEALTH_PORT": "8117",
    }.items():
        monkeypatch.setitem(os.environ, key, value)

    settings = config.Settings()

    assert not hasattr(settings, "physical_backup")


def test_runner_profile_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    s = config.Settings(profile="runner")
    for domain in (
        "services",
        "daemon",
        "general",
        "data_plane",
        "gateway",
        "lm",
        "sandbox",
        "observability",
    ):
        assert s.has_domain(domain), domain
    for domain in ("agent", "web", "alerts", "telegram", "feishu"):
        assert not s.has_domain(domain), domain
        with pytest.raises(AttributeError):
            getattr(s, domain)


def test_settings_reads_env_profile_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    monkeypatch.setenv(config.MEIVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings()
    assert not s.has_domain("agent")
    monkeypatch.delenv(config.MEIVA_PROCESS_PROFILE_ENV, raising=False)
    s2 = config.Settings()
    assert s2.has_domain("agent") and s2.has_domain("sandbox") and s2.has_domain("web")


def test_unknown_profile_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    with pytest.raises(ValueError):
        config.Settings(profile="bogus")


def test_explicit_none_profile_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import config

    monkeypatch.setenv(config.MEIVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings(profile=None)
    assert s.has_domain("agent") and s.has_domain("sandbox") and s.has_domain("web")


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_and_panel_full_under_real_gateway_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base import config

    authority = ConfigAuthority(config.settings, config.settings, tmp_path / ".env")
    baseline_bootstrap = authority.bootstrap_config_values(
        provider_key_envs=(), plugin_cluster_config=""
    )
    baseline_values = authority.current_field_values()
    baseline_flat = authority.flat_dump(mode="json")

    excluded_domains = ("agent", "sandbox", "web")
    excluded_bootstrap_aliases = {
        config.field_alias(n)
        for n in config.BOOTSTRAP_FIELDS
        if config.field_domain(n) in excluded_domains
    }

    limited = config.Settings(profile="gateway")
    monkeypatch.setattr(config, "settings", limited)

    authority = ConfigAuthority(limited, authority.all_domains, authority.env_path)
    served = authority.bootstrap_config_values(provider_key_envs=(), plugin_cluster_config="")
    values = authority.current_field_values()
    flat = authority.flat_dump(mode="json")

    assert set(served) == set(baseline_bootstrap)
    assert set(values) == set(baseline_values)
    assert set(flat) == set(baseline_flat)

    for alias in excluded_bootstrap_aliases:
        if alias in baseline_bootstrap:
            assert served[alias] == baseline_bootstrap[alias], alias
    for name in baseline_values:
        if config.field_domain(name) in excluded_domains:
            assert values[name] == baseline_values[name], name

    with pytest.raises(AttributeError):
        _ = config.settings.sandbox


def test_memory_backend_switch_fields_require_gateway_restart() -> None:
    from base.config.domains.services.settings import ServiceSettings
    from base.config.profiles import PROCESS_PROFILES

    assert "services" in PROCESS_PROFILES["gateway"]

    for name in ("memory_search_backend",):
        field = ServiceSettings.model_fields[name]
        extra = field.json_schema_extra
        assert isinstance(extra, dict)
        assert extra["restart_required"] == "gateway", name


def test_gateway_consumed_fields_declare_gateway_restart() -> None:
    from base.config.domains.channels.feishu import FeishuSettings
    from base.config.domains.channels.telegram import TelegramSettings
    from base.config.domains.lm import LmSettings
    from base.config.domains.services.settings import ServiceSettings
    from base.config.profiles import PROCESS_PROFILES

    for domain in ("services", "telegram", "feishu", "lm"):
        assert domain in PROCESS_PROFILES["gateway"]

    by_model = {
        ServiceSettings: (
            "embedding_backend",
            "memory_embed_timeout_seconds",
            "im_disabled_adapters",
            "im_send_retry_delays",
            "im_sse_read_timeout_seconds",
        ),
        TelegramSettings: (
            "telegram_bot_token",
            "telegram_owner_id",
            "telegram_poll_timeout_seconds",
            "telegram_reconnect_base_delay_seconds",
            "telegram_reconnect_max_delay_seconds",
        ),
        FeishuSettings: (
            "feishu_app_id",
            "feishu_app_secret",
            "feishu_rest_timeout_seconds",
        ),
        LmSettings: (
            "labeler_model",
            "reply_suggestion_enabled",
            "reply_suggestion_model",
            "reply_suggestion_gate_model",
            "reply_suggestion_gate_threshold",
            "reply_suggestion_max_input_tokens",
            "reply_suggestion_max_candidates",
        ),
    }
    for model, names in by_model.items():
        for name in names:
            extra = model.model_fields[name].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["restart_required"] == "gateway", name


def test_hierarchy_model_declares_all_restart() -> None:
    from base.config.domains.lm import LmSettings

    extra = LmSettings.model_fields["hierarchy_model"].json_schema_extra
    assert isinstance(extra, dict)
    assert extra["restart_required"] == "all"


def test_build_registry_rejects_unknown_restart_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import Field

    from base.config import config_registry
    from base.config.base import EnvSettings

    class Bad(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="MEIVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "gatewat"},
        )

    monkeypatch.setattr(
        config_registry,
        "DOMAIN_MODELS",
        (("synthetic", "Synthetic", Bad, "agent-runner"),),
    )
    config_registry.build_registry.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="restart_required='gatewat'"):
            config_registry.build_registry()
    finally:
        config_registry.build_registry.cache_clear()


def test_build_registry_rejects_restart_required_for_unconsuming_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import Field

    from base.config import config_registry
    from base.config.base import EnvSettings

    class WrongKind(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="MEIVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "agent"},
        )

    monkeypatch.setattr(
        config_registry,
        "DOMAIN_MODELS",
        (("telegram", "Telegram", WrongKind, "gateway"),),
    )
    config_registry.build_registry.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=r"restart_required='agent'.*process profile"):
            config_registry.build_registry()
    finally:
        config_registry.build_registry.cache_clear()


def test_every_field_restart_required_names_a_kind_that_consumes_it() -> None:
    from typing import Any, cast

    from base.config import FIELD_REFS
    from base.config.config_registry import ALLOWED_RESTART_REQUIRED, RESTART_REQUIRED_PROFILE
    from base.config.profiles import PROCESS_PROFILES, ProcessProfile

    def _extra(ref: object) -> dict[str, Any]:
        info = getattr(ref, "info", ref)
        extra = getattr(info, "json_schema_extra", None)
        return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}

    bad_value: list[tuple[str, str]] = [
        (name, str(_extra(ref).get("restart_required", ""))) for name, ref in FIELD_REFS.items()
    ]
    bad_value = [(n, v) for n, v in bad_value if v not in ALLOWED_RESTART_REQUIRED]
    assert not bad_value, f"fields with invalid restart_required: {bad_value}"

    wrong_kind: list[tuple[str, str, str]] = []
    for name, ref in FIELD_REFS.items():
        restart = str(_extra(ref).get("restart_required", ""))
        kind = RESTART_REQUIRED_PROFILE.get(restart)
        if kind is not None and ref.domain not in PROCESS_PROFILES[cast(ProcessProfile, kind)]:
            wrong_kind.append((name, ref.domain, restart))
    assert not wrong_kind, (
        f"fields whose restart_required names a process kind that does not consume "
        f"them: {wrong_kind}"
    )
    assert "schedule" in ALLOWED_RESTART_REQUIRED
