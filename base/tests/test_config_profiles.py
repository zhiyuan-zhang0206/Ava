"""Per-process profile construction and fail-fast, the D5 full-read guarantee, and restart_required declarations; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import pytest

# ── D5: profile-limited singleton must not shrink the config-service reads ──
#
# Under per-process profiles (AVA_PROCESS_PROFILE, Task #856 Phase B) the
# module singleton constructs only its profile's sub-models and raises
# AttributeError on a domain outside the profile (fail-fast). The
# config-SERVICE read paths — bootstrap_config_values (the gateway serves
# every BOOTSTRAP_FIELDS to agent-runners), current_field_values (the 231-field
# config panel) and flat_dump (the config-overlay snapshot) — must stay
# complete, so they resolve a missing domain through a fresh full Settings
# instance (D5). These tests simulate a profile-limited singleton with a proxy
# that raises exactly like the PR-B construction will.


@pytest.mark.usefixtures("served_gateway_home")
def test_service_reads_stay_full_when_singleton_domain_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap / current_field_values / flat_dump serve the same complete
    payload with a gateway-profile-limited singleton as without one."""
    from base import config

    real = config.settings

    class _GatewayProfileLimited:
        """Stand-in for the PR-B gateway-profile singleton: the domains the
        gateway profile excludes raise AttributeError (fail-fast); every other
        domain reads through to the real singleton."""

        def __getattr__(self, name: str):
            if name in ("agent", "sandbox", "web"):
                raise AttributeError(
                    f"gateway profile does not construct the {name} domain (Task #856)"
                )
            return getattr(real, name)

    baseline_bootstrap = config.bootstrap_config_values()
    baseline_values = config.current_field_values()
    baseline_flat = config.flat_dump(mode="json")

    monkeypatch.setattr(config, "settings", _GatewayProfileLimited())

    served = config.bootstrap_config_values()
    values = config.current_field_values()
    flat = config.flat_dump(mode="json")

    # Same payload — the full-instance fallback reads the same os.environ the
    # singleton did, so nothing may change by swapping in a limited singleton.
    assert served == baseline_bootstrap
    assert values == baseline_values
    assert flat == baseline_flat

    # The excluded domains' fields are genuinely served (not accidentally
    # absent from both sides): spot-check one field per excluded domain.
    for name in ("exec_timeout_seconds", "prompt_invest_future_enabled", "web_fetch_model"):
        assert name in values, name
        assert name in flat, name
    assert "AVA_EXEC_TIMEOUT_SECONDS" in served


def test_get_field_stays_strict_on_excluded_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_field` (the reflective escape hatch) must NOT silently fall back to
    the full instance — fail-fast is the point of the profile boundary; only
    the config-service read paths are profile-independent (D5)."""
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


# ── Per-process profiles (Task #856 Phase B): construction + fail-fast ──


def _profile_settings(monkeypatch: pytest.MonkeyPatch, profile: str):
    """Build a fresh Settings for `profile` (never the env marker)."""
    from base import config

    return config.Settings(profile=profile)


def test_gateway_profile_excludes_agent_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway profile: sandbox/agent/web are NOT constructed — access raises an
    actionable AttributeError; lm/telegram/feishu ARE (real gateway-side reads)."""
    from base import config

    s = config.Settings(profile="gateway")
    for domain in ("sandbox", "agent", "web"):
        assert not s.has_domain(domain)
        with pytest.raises(AttributeError) as exc:
            getattr(s, domain)
        assert "gateway" in str(exc.value) and domain in str(exc.value)
        assert "has_domain" in str(exc.value)  # actionable: points at the escape hatch
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
    """Agent profile constructs its matrix domains; alerts (the gateway ingest's),
    telegram and feishu remain excluded."""
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
    """A unit `.env` that still carries the deleted PITR stack's keys must not stop
    Settings from building: every sub-model ignores keys it does not declare, so
    an upgrade never has to rewrite a home's `.env` before it can start."""
    import os

    from base import config

    for key, value in {
        "AVA_PITR_ENABLED": "true",
        "AVA_PITR_STORE_BACKEND": "oss",
        "AVA_PITR_OSS_BUCKET": "retired",
        "AVA_PITR_GCS_PREFIX": "ava-pitr/retired",
        "AVA_PITR_SPOOL_HARD_BYTES": "2362232013",
        "AVA_PITR_UPLOADER_HEALTH_PORT": "8117",
    }.items():
        monkeypatch.setitem(os.environ, key, value)

    settings = config.Settings()

    assert not hasattr(settings, "physical_backup")


def test_runner_profile_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Support daemons include browser sandbox and telemetry; alerts belong to the
    gateway's ingest, and agent execution stays in the agent-host's separate profile."""
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
    """A plain Settings() reads AVA_PROCESS_PROFILE from the environment; with
    the marker absent it constructs every domain (unchanged behavior)."""
    from base import config

    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings()
    assert not s.has_domain("agent")
    monkeypatch.delenv(config.AVA_PROCESS_PROFILE_ENV, raising=False)
    s2 = config.Settings()
    assert s2.has_domain("agent") and s2.has_domain("sandbox") and s2.has_domain("web")


def test_unknown_profile_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown profile name is a launcher bug — fail at construction."""
    from base import config

    with pytest.raises(ValueError):
        config.Settings(profile="bogus")


def test_explicit_none_profile_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """profile=None (the config-service read paths, D5) builds every domain even
    when the environment carries a profile marker."""
    from base import config

    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings(profile=None)
    assert s.has_domain("agent") and s.has_domain("sandbox") and s.has_domain("web")


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_and_panel_full_under_real_gateway_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5 end-to-end with the REAL profile-limited singleton: swap the module
    singleton for a gateway-profile Settings (agent/sandbox/web excluded) and
    the bootstrap payload + config panel + overlay snapshot stay COMPLETE, with
    the excluded domains' values identical to the full singleton's.

    (In-profile domain values may differ from the pre-swap baseline here
    because conftest mutates the import-time singleton by setattr — a test
    artifact; in a real gateway process both instances read the same
    os.environ, so the values coincide.)"""
    from base import config

    baseline_bootstrap = config.bootstrap_config_values()
    baseline_values = config.current_field_values()
    baseline_flat = config.flat_dump(mode="json")

    excluded_domains = ("agent", "sandbox", "web")
    excluded_bootstrap_aliases = {
        config.field_alias(n)
        for n in config.BOOTSTRAP_FIELDS
        if config.field_domain(n) in excluded_domains
    }

    limited = config.Settings(profile="gateway")
    monkeypatch.setattr(config, "settings", limited)

    served = config.bootstrap_config_values()
    values = config.current_field_values()
    flat = config.flat_dump(mode="json")

    # Completeness: nothing that was served before disappears under the
    # profile-limited singleton (the gateway serves all 162 BOOTSTRAP_FIELDS).
    assert set(served) == set(baseline_bootstrap)
    assert set(values) == set(baseline_values)
    assert set(flat) == set(baseline_flat)
    # Excluded-domain values: identical to the full singleton's (same env).
    for alias in excluded_bootstrap_aliases:
        if alias in baseline_bootstrap:  # None-valued fields are skipped in both
            assert served[alias] == baseline_bootstrap[alias], alias
    for name in baseline_values:
        if config.field_domain(name) in excluded_domains:
            assert values[name] == baseline_values[name], name
    # the fail-fast still protects direct attribute access on the singleton
    with pytest.raises(AttributeError):
        _ = config.settings.sandbox


# ─── frontend config-group map alignment (ui/web/src/app/control/_config_groups.ts) ───


# ─── memory backend switch fields: restart_required must name "gateway" ───


def test_memory_backend_switch_fields_require_gateway_restart() -> None:
    """The backend-switch fields must declare restart_required="gateway".

    AVA_MEMORY_SEARCH_BACKEND is consumed at process boot by the gateway
    search endpoint (factory.get_backend) and the memory_indexer daemon;
    switching to 'numpy' also makes the gateway's search path ride the
    memory_search daemon. All three processes run under the gateway process
    profile, so the metadata must name "gateway" — an `ava restart` bounces
    the gateway process AND every gateway-profile daemon. A "" here told the
    panel/CLI "no restart required" and a backend switch silently stayed
    unapplied until a manual kickstart (Task #2224).
    """
    from base.config.domains.services.settings import ServiceSettings
    from base.config.profiles import PROCESS_PROFILES

    # The owning domain must be in the gateway profile for "gateway" to be the
    # honest value — the consumption matrix, kept in sync with the profile set.
    assert "services" in PROCESS_PROFILES["gateway"]

    for name in ("memory_search_backend",):
        field = ServiceSettings.model_fields[name]
        extra = field.json_schema_extra
        assert isinstance(extra, dict)
        assert extra["restart_required"] == "gateway", name


# ─── gateway-consumed fields must declare "gateway" restart (batch audit) ───


def test_gateway_consumed_fields_declare_gateway_restart() -> None:
    """Fields read at boot by gateway-profile processes must say "gateway".

    Every field here is consumed by a gateway-profile process (the gateway
    process itself or a gateway-side daemon: im_bridge / memory_indexer /
    memory_search). A wrong or empty value tells the panel/CLI the wrong
    process to restart (Task #2224 follow-up audit):

    - im_* / telegram / feishu: the im_bridge daemon reads them at boot —
      they said "agent", so an operator restarted the agent process and the
      change never applied (the bcf966476 fix was lost in the main rebuild).
    - embedding_backend / memory_embed_timeout_seconds: the memory_indexer /
      memory_search daemons read them at boot — they said "" (no restart hint
      at all), the same class #2224 fixed for memory_search_backend.
    """
    from base.config.domains.channels.feishu import FeishuSettings
    from base.config.domains.channels.telegram import TelegramSettings
    from base.config.domains.services.settings import ServiceSettings
    from base.config.profiles import PROCESS_PROFILES

    # The owning domains must be in the gateway profile for "gateway" to be
    # the honest value — the consumption matrix, kept in sync with the set.
    for domain in ("services", "telegram", "feishu"):
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
    }
    for model, names in by_model.items():
        for name in names:
            extra = model.model_fields[name].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["restart_required"] == "gateway", name


# ─── restart_required: value domain + consumption-matrix cross-check ───


def test_build_registry_rejects_unknown_restart_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd restart_required can never reach prod boot — it is the operator's
    only guide for which process to restart, so a bad value must fail fast at
    registry build, the same seal scope/capability/lifecycle have (the bcf966476
    fail-fast, lost in the main rebuild and restored by #2227)."""
    from pydantic import Field

    from base.config.base import EnvSettings
    from base.host.env import config_registry

    class Bad(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="AVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "gatewat"},
        )

    monkeypatch.setattr(
        config_registry,
        "DOMAIN_MODELS",
        (("synthetic", "Synthetic", Bad, "agent-runner"),),
    )
    config_registry._build_registry.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="restart_required='gatewat'"):
            config_registry._build_registry()
    finally:
        config_registry._build_registry.cache_clear()


def test_build_registry_rejects_restart_required_for_unconsuming_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart_required names a process kind — that kind's config profile must
    contain the field's domain. The telegram/feishu/im_* fields once said "agent"
    while only the gateway's im_bridge daemon reads them: the operator restarted
    the wrong process and the change silently never took effect (#1226 re-landed
    the value fixes; this check makes the drift class impossible)."""
    from pydantic import Field

    from base.config.base import EnvSettings
    from base.host.env import config_registry

    class WrongKind(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="AVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "agent"},
        )

    monkeypatch.setattr(
        config_registry,
        "DOMAIN_MODELS",
        (("telegram", "Telegram", WrongKind, "gateway"),),
    )
    config_registry._build_registry.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=r"restart_required='agent'.*process profile"):
            config_registry._build_registry()
    finally:
        config_registry._build_registry.cache_clear()


def test_every_field_restart_required_names_a_kind_that_consumes_it() -> None:
    """Metadata-surface belt-and-braces over the registry enforcement: every
    field's restart_required is in the value domain, and when it names a process
    kind, the field's domain is in that kind's profile (the profile sets ARE the
    consumption matrix — test_gateway_consumer_guard keeps them honest)."""
    from typing import Any, cast

    from base.config import _FIELDS
    from base.config.profiles import PROCESS_PROFILES
    from base.host.env.config_registry import _ALLOWED_RESTART_REQUIRED, _RESTART_REQUIRED_PROFILE

    def _extra(ref: object) -> dict[str, Any]:
        info = getattr(ref, "info", ref)
        extra = getattr(info, "json_schema_extra", None)
        return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}

    bad_value: list[tuple[str, str]] = [
        (name, str(_extra(ref).get("restart_required", ""))) for name, ref in _FIELDS.items()
    ]
    bad_value = [(n, v) for n, v in bad_value if v not in _ALLOWED_RESTART_REQUIRED]
    assert not bad_value, f"fields with invalid restart_required: {bad_value}"

    wrong_kind: list[tuple[str, str, str]] = []
    for name, ref in _FIELDS.items():
        restart = str(_extra(ref).get("restart_required", ""))
        kind = _RESTART_REQUIRED_PROFILE.get(restart)
        if kind is not None and ref.domain not in PROCESS_PROFILES[kind]:  # type: ignore[index]
            wrong_kind.append((name, ref.domain, restart))
    assert not wrong_kind, (
        f"fields whose restart_required names a process kind that does not consume "
        f"them: {wrong_kind}"
    )
    assert "schedule" in _ALLOWED_RESTART_REQUIRED
