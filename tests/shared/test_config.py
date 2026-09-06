"""warn_deprecated_env_aliases nudges operators off the legacy gateway-url name.

AVA_PRIMARY_GATEWAY_URL was renamed AVA_GATEWAY_URL; the old name
still resolves but is scheduled for removal. The startup warning must fire only
when the old name is the active source (old set, new unset).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import shared.log
from shared import config


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


def _patch_logger(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    rec = _RecordingLogger()
    # warn_deprecated_env_aliases does `from shared.log import logger` at call
    # time, so patching the module attribute is enough.
    monkeypatch.setattr(shared.log, "logger", rec)
    return rec


def test_warns_when_only_deprecated_name_set(monkeypatch: pytest.MonkeyPatch):
    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("AVA_PRIMARY_GATEWAY_URL", "https://gw.example")
    monkeypatch.delenv("AVA_GATEWAY_URL", raising=False)

    config.warn_deprecated_env_aliases()

    assert len(rec.warnings) == 1
    assert "AVA_PRIMARY_GATEWAY_URL" in rec.warnings[0]
    assert "2026-09-01" in rec.warnings[0]


def test_silent_when_canonical_name_set(monkeypatch: pytest.MonkeyPatch):
    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("AVA_PRIMARY_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("AVA_GATEWAY_URL", "https://gw.example")

    config.warn_deprecated_env_aliases()

    assert rec.warnings == []


def test_silent_when_neither_set(monkeypatch: pytest.MonkeyPatch):
    rec = _patch_logger(monkeypatch)
    monkeypatch.delenv("AVA_PRIMARY_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AVA_GATEWAY_URL", raising=False)

    config.warn_deprecated_env_aliases()

    assert rec.warnings == []


def test_warns_when_only_skip_auth_alias_set(monkeypatch: pytest.MonkeyPatch):
    """AVA_SKIP_AUTH / AVA_SKIP_SECURITY_SCAN are deprecated with INVERTED
    semantics — the warning must fire when the legacy key is the active source."""
    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("AVA_SKIP_AUTH", "true")
    # delitem, not delenv: the lint bans delenv on Settings aliases (the
    # singleton never re-reads env), but this function inspects the RAW env.
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)

    config.warn_deprecated_env_aliases()

    assert len(rec.warnings) == 1
    assert "AVA_SKIP_AUTH" in rec.warnings[0]
    assert "INVERTED" in rec.warnings[0]


def test_silent_when_skip_canonical_alias_set(monkeypatch: pytest.MonkeyPatch):
    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("AVA_SKIP_AUTH", "true")
    # setitem, not setenv: see test_warns_when_only_skip_auth_alias_set.
    monkeypatch.setitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", "true")

    config.warn_deprecated_env_aliases()

    assert rec.warnings == []


def test_openai_api_key_field_exists_and_is_per_secret():
    from shared.config import FIELD_INFOS, field_domain

    field = FIELD_INFOS["openai_api_key"]
    assert field.alias == "OPENAI_API_KEY"
    extra = field.json_schema_extra
    assert isinstance(extra, dict)  # narrow JsonDict | Callable | None for subscript
    assert extra["sensitive"] is True
    # A provider key is owned by the LLM domain (group is derived from the sub-model).
    assert field_domain("openai_api_key") == "lm"


def test_retired_runner_selector_is_not_configurable() -> None:
    """Only the agent host runs agents; no UI or overlay can select the old runner."""
    from shared.config import FIELD_INFOS, get_config_metadata

    assert "runner_mode" not in FIELD_INFOS
    assert all(meta.env_var != "AVA_RUNNER_MODE" for meta in get_config_metadata())
    assert "restarter_poll_interval_seconds" not in FIELD_INFOS


def test_skill_match_fields_are_unregistered() -> None:
    """The deleted skill semantic matcher leaves no settings residue.

    The matcher was removed per user ruling 2026-08-27; a later merge
    accidentally restored its fields on main and they re-entered the config
    surface (Inspector Configuration Overlay) and per-agent overlay
    acceptance. Regression guard: the four keys must stay unregistered —
    a resurrected field would surface in the config UI again and be accepted
    in `ava.self.restart(config_overlay=...)`.
    """
    from shared.config import FIELD_INFOS

    for name in (
        "skill_match_enabled",
        "skill_match_top_k",
        "skill_match_min_score",
        "skill_match_budget_ms",
    ):
        assert name not in FIELD_INFOS, f"{name} must not be a registered settings field"


@pytest.mark.parametrize("raw", ["[1, 2.5]", "1,2.5"])
def test_delivery_watchdog_backoff_accepts_json_or_comma_list(raw: str) -> None:
    from shared.config.daemon import DaemonSettings

    configured = DaemonSettings.model_validate(
        {"AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S": raw}
    )
    assert configured.delivery_watchdog_dispatch_backoff_steps_s == [1.0, 2.5]


@pytest.mark.parametrize("raw", ["", "[0]", "[-1, 2]"])
def test_delivery_watchdog_backoff_rejects_empty_or_nonpositive_steps(raw: str) -> None:
    import pydantic

    from shared.config.daemon import DaemonSettings

    with pytest.raises(pydantic.ValidationError):
        DaemonSettings.model_validate({"AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S": raw})


def test_delivery_watchdog_wake_suppression_defaults() -> None:
    from shared.config.daemon import DaemonSettings

    configured = DaemonSettings()
    assert configured.delivery_watchdog_resurrect_fail_before_suppress == 5
    assert configured.delivery_watchdog_suppress_base_seconds == 1800.0
    assert configured.delivery_watchdog_suppress_max_seconds == 86400.0


def test_current_field_values_coerces_secretstr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A SecretStr field read from .env must come back a SecretStr, not a bare str
    — `.get_secret_value()` consumers crash on a plain str."""
    from pydantic import SecretStr

    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"anthropic_api_key": "sk-ant-abc"}, set())

    secret = config.current_field_values()["anthropic_api_key"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "sk-ant-abc"


def test_current_field_values_coerces_bool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A bool field written to .env round-trips through the field type unchanged."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": True}, set())

    assert config.current_field_values()["trace_enabled"] is True


def test_bootstrap_serves_comma_list_not_repr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A NoDecode comma-list field set in .env reaches an agent as the raw "a,b"
    env text, not a Python list repr (which the agent would split into garbage)."""
    from shared import runtime_config as rt
    from shared.envfile import upsert_env

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())
    upsert_env(
        tmp_path / ".env",
        {
            "AVA_DB_URL": str(config.settings.data_plane.db_url),
            "AVA_RUNNER_DB_PASSWORD": "runner-password",
        },
    )

    vals = config.bootstrap_config_values()
    assert vals["AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT"] == "alpha,beta"


def test_current_field_values_decodes_nodecode_comma_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A NoDecode comma-list field reads back as a list (split like the model's
    _split_comma_list validator), not a silently mis-typed raw string."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())

    v = config.current_field_values()["skills_to_inject_into_system_prompt"]
    assert v == ["alpha", "beta"]


def test_current_field_values_decodes_nodecode_comma_list_without_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """P1 regression: a NoDecode comma-list value is a SUPPORTED .env spelling,
    not a decode failure. Decoding through the owning model must not emit the
    "cannot be decoded" warning — it fired on every panel read / agent spawn
    for AVA_IM_DISABLED_ADAPTERS='weixin,feishu' — and a list[float] field must
    come back floats, not a wrong-typed string split."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields(
        {
            "im_disabled_adapters": ["weixin", "feishu"],
            "im_send_retry_delays": [0.5, 1.0],
        },
        set(),
    )

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert values["im_send_retry_delays"] == [0.5, 1.0]
    assert all(isinstance(delay, float) for delay in values["im_send_retry_delays"])
    assert rec.warnings == []


def test_current_field_values_decodes_json_array_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The JSON-array spelling the model validators also accept must decode
    through the panel path too — both spellings, not just the comma list."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": '["weixin", "feishu"]'}, set())

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


def test_current_field_values_decodes_empty_nodecode_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An empty NoDecode list value decodes to [] (nothing disabled), matching
    Settings construction."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": []}, set())

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == []
    assert rec.warnings == []


def test_auth_middleware_set_roundtrips_through_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`ava config set auth_middleware_enabled` writes the key the model
    actually reads. The field's alias used to fall back to its upper-cased
    NAME (AUTH_MIDDLEWARE_ENABLED), which a validation_alias-only field never
    listens on — a written value was silently lost and the panel never served
    the file value."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"auth_middleware_enabled": False}, set())

    text = (tmp_path / ".env").read_text()
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert keys == {"AVA_AUTH_MIDDLEWARE_ENABLED"}
    assert config.current_field_values()["auth_middleware_enabled"] is False


def test_every_field_declares_valid_scope() -> None:
    """Every Settings field must declare an ownership scope in json_schema_extra."""
    from shared.config import FIELD_INFOS

    allowed = {"cluster-pinned", "cluster-default", "host", "agent"}
    missing: list[str] = []
    invalid: list[tuple[str, object]] = []
    for name, field in FIELD_INFOS.items():
        extra = field.json_schema_extra
        assert isinstance(extra, dict), f"{name}: json_schema_extra must be a dict"
        if "scope" not in extra:
            missing.append(name)
        elif extra["scope"] not in allowed:
            invalid.append((name, extra["scope"]))  # pyright: ignore[reportUnknownArgumentType]
    assert not missing, f"fields missing scope=: {missing}"
    assert not invalid, f"fields with invalid scope: {invalid}"


def test_every_field_resolves_a_valid_capability() -> None:
    """Every field resolves to a capability in the allowed set — the top-level
    config-panel section. Resolution is the field's `capability` override or its
    domain default; `_build_registry` fail-fasts on a bad value at import, so this
    guards the public metadata surface the frontend groups on."""
    from shared.config import get_config_metadata
    from shared.config_registry import _ALLOWED_CAPABILITIES

    assert frozenset({"gateway", "agent-runner", "common"}) == _ALLOWED_CAPABILITIES
    bad = [
        (m.name, m.capability)
        for m in get_config_metadata()
        if m.capability not in _ALLOWED_CAPABILITIES
    ]
    assert not bad, f"fields with invalid capability: {bad}"


def test_capability_assignment_is_pinned_for_load_bearing_fields() -> None:
    """Spot-check the capability of one field per case so a future edit that
    misgroups a load-bearing field (or breaks the domain-default / override
    resolution) fails loudly here. Covers: domain default (gateway_port -> gateway
    with no override), the mixed-domain override (ops_concurrency /
    host_max_concurrent_turns -> agent-runner though their domain defaults to
    gateway), agent-runtime config (llm_model -> agent-runner), the data plane
    (db_url -> gateway), and cluster-wide policy / host identity (timezone,
    machine_host -> common)."""
    from shared.config import get_config_metadata

    cap = {m.name: m.capability for m in get_config_metadata()}
    expected = {
        "gateway_port": "gateway",  # gateway domain default, no override
        "db_url": "gateway",  # cluster-pinned data-plane field, still gateway-owned
        "llm_model": "common",  # cluster-wide spawn default — read by the gateway
        # (spawn pre-select / default-model endpoint) AND frozen into agents at
        # spawn; moved from agent-runner on 2026-08-06 so it survives the
        # gateway profile pop (P0: AVA_MODEL popped broke spawn defaults).
        "exec_timeout_seconds": "agent-runner",
        "ops_concurrency": "agent-runner",  # services domain (default gateway) override
        "host_max_concurrent_turns": "agent-runner",  # daemon domain (default gateway) override
        "browser_enabled": "agent-runner",
        "timezone": "common",  # cluster-wide policy
        "machine_host": "common",  # shared host identity
        "trace_tags": "common",  # observability domain default
    }
    for name, want in expected.items():
        assert cap[name] == want, f"{name}: capability drifted to {cap[name]!r} (want {want!r})"


def test_build_registry_rejects_bad_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd capability can never reach prod boot. `_build_registry` runs at
    import and raises on a bad resolved capability (a field override or, here, a
    domain default), so the whole process fails fast rather than silently
    mis-grouping — the same seal `scope` has. Pin the raise directly (the
    all-fields-valid test above only proves the current tree is clean)."""
    from shared import config_registry

    # The registry builds lazily on first use and memoizes; patch the module
    # (not the config re-export) and clear the cache so the typo is exercised.
    monkeypatch.setattr(
        config_registry,
        "_DOMAIN_MODELS",
        (("telegram", "Telegram", "TelegramSettings", "gatway"),),  # typo'd default
    )
    config_registry._build_registry.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="capability='gatway'"):
            config_registry._build_registry()
    finally:
        config_registry._build_registry.cache_clear()


def test_identity_fields_are_read_only() -> None:
    """Cluster-identity connection strings must not be UI-writable (footgun)."""
    from shared.config import FIELD_INFOS

    for name in ("db_url", "redis_url"):
        extra = FIELD_INFOS[name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["writable"] is False, f"{name} must be writable=False"


def test_host_identity_fields_are_read_only() -> None:
    """Host identity / connection / infra fields are not panel-writable — they're set
    via install / enroll / `ava start`, not the runtime config panel. (The config
    write path enforces this too; the metadata must agree so the UI shows read-only.)"""
    from shared.config import FIELD_INFOS

    for name in (
        "ava_home",
        "gateway_url",
        "gateway_port",
        "machine_name",
        "machine_serve_gateway",
        "machine_serve_agent_runner",
        "machine_host",
    ):
        extra = FIELD_INFOS[name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["writable"] is False, f"{name} must be writable=False"


def test_bootstrap_fields_derived_from_scope() -> None:
    """BOOTSTRAP_FIELDS == exactly the cluster-scoped fields, derived not hand-listed."""
    from shared.config import BOOTSTRAP_FIELDS, FIELD_INFOS

    derived = {
        name
        for name, field in FIELD_INFOS.items()
        if isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("scope") in ("cluster-pinned", "cluster-default")  # pyright: ignore[reportUnknownMemberType]
        and field.json_schema_extra.get("bootstrap", True) is not False  # pyright: ignore[reportUnknownMemberType]
    }
    assert set(BOOTSTRAP_FIELDS) == derived
    # cluster identity + a behavior knob both present (the intended delta);
    # host / agent fields excluded.
    assert {"db_url", "llm_model", "exec_timeout_seconds"} <= set(BOOTSTRAP_FIELDS)
    assert not ({"db_admin_password", "redis_admin_password"} & set(BOOTSTRAP_FIELDS))
    assert not ({"machine_name", "gateway_pidfile", "sdk_disable"} & set(BOOTSTRAP_FIELDS))


def test_physical_backup_cluster_pinned_fields_are_never_bootstrap_served() -> None:
    """A runner must never receive backup enablement without its host credentials."""
    from shared.config import BOOTSTRAP_FIELDS, FIELD_INFOS, field_domain

    physical_backup_cluster_pinned = {
        name
        for name, field in FIELD_INFOS.items()
        if field_domain(name) == "physical_backup"
        and isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("scope") == "cluster-pinned"  # pyright: ignore[reportUnknownMemberType]
    }

    assert physical_backup_cluster_pinned
    assert not (set(BOOTSTRAP_FIELDS) & physical_backup_cluster_pinned)


def test_bootstrap_distributes_a_behavior_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default agent-behavior knob is now distributed to agent-runners."""
    from shared import config as cfg
    from shared import runtime_config

    # .env file may carry a stale value from another test; bypass it so the
    # monkeypatched settings value is the only source.
    monkeypatch.setattr(
        runtime_config,
        "read_env_aliases",
        lambda: {
            "AVA_DB_URL": str(cfg.settings.data_plane.db_url),
            "AVA_RUNNER_DB_PASSWORD": "runner-password",
        },
    )
    monkeypatch.setattr(cfg.settings.sandbox, "exec_timeout_seconds", 123.0)
    values = cfg.bootstrap_config_values()
    assert values["AVA_EXEC_TIMEOUT_SECONDS"] == "123.0"


def test_cluster_default_iff_per_agent() -> None:
    """scope=cluster-default fields must be marked per_agent=True (one-way, not iff)."""
    from shared.config import FIELD_INFOS

    for name, field in FIELD_INFOS.items():
        extra = field.json_schema_extra
        assert isinstance(extra, dict), f"{name}: json_schema_extra must be a dict"
        is_default = extra.get("scope") == "cluster-default"  # pyright: ignore[reportUnknownMemberType]
        is_per_agent = extra.get("per_agent") is True  # pyright: ignore[reportUnknownMemberType]
        if is_default:
            assert is_per_agent, (
                f"{name}: scope=cluster-default but per_agent is not True — "
                "a cluster-default Settings field must be marked per_agent=True"
            )


def test_host_fields_declare_remote_writable_bool() -> None:
    """Every host-scope Settings field must declare a bool remote_writable."""
    from shared.config import FIELD_INFOS

    for name, field in FIELD_INFOS.items():
        extra = field.json_schema_extra
        assert isinstance(extra, dict), f"{name}: json_schema_extra must be a dict"
        if extra.get("scope") == "host":  # pyright: ignore[reportUnknownMemberType]
            assert isinstance(extra.get("remote_writable"), bool), (  # pyright: ignore[reportUnknownMemberType]
                f"{name}: host-scope field must declare remote_writable: bool, "
                f"got {extra.get('remote_writable')!r}"  # pyright: ignore[reportUnknownMemberType]
            )


_REMOTE_WRITABLE_ALLOWLIST = frozenset(
    {
        "auto_resurrect_enabled",
        "browser_enabled",
        "chrome_binary",
        "computer_use_lease_s",
        "computer_use_queue_timeout_s",
        "computer_use_session_idle_s",
        "delivery_watchdog_enabled",
        "heartbeat_enabled",
        "machine_description",
        "permissions_helper_enabled",
        "permissions_helper_spawn",
        "ops_concurrency",
        "task_maintenance_enabled",
        "watchdog_interval_seconds",
        "watchdog_respawn_backoff_cap_seconds",
        "watchdog_respawn_breaker_rounds",
    }
)


def test_remote_writable_allowlist_is_exact() -> None:
    """Exactly the allowlist fields are remote_writable=True; all others are False."""
    from shared.config import FIELD_INFOS

    actual_true = {
        name
        for name, field in FIELD_INFOS.items()
        if isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("remote_writable") is True  # pyright: ignore[reportUnknownMemberType]
    }
    assert actual_true == _REMOTE_WRITABLE_ALLOWLIST, (
        f"remote_writable=True fields mismatch.\n"
        f"  expected: {sorted(_REMOTE_WRITABLE_ALLOWLIST)}\n"
        f"  actual:   {sorted(actual_true)}"
    )


def test_non_host_fields_not_remote_writable_true() -> None:
    """No non-host-scope field may set remote_writable: True."""
    from shared.config import FIELD_INFOS

    violations = [
        name
        for name, field in FIELD_INFOS.items()
        if isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("scope") != "host"  # pyright: ignore[reportUnknownMemberType]
        and field.json_schema_extra.get("remote_writable") is True  # pyright: ignore[reportUnknownMemberType]
    ]
    assert not violations, (
        f"Non-host fields with remote_writable=True: {violations}. "
        "Only host-scope fields may be remote_writable."
    )


def test_no_agent_scope_field_is_writable() -> None:
    """Every agent-scope field must have writable=False.

    Agent-scope fields are read from env vars at process startup and are not
    stored in cluster or host override storage. Marking any of them writable=True
    would allow the config PUT to accept them, but there is no store to route them
    to. The writable gate enforces this invariant at the field-metadata layer.
    """
    from shared.config import FIELD_INFOS

    violations = [
        name
        for name, field in FIELD_INFOS.items()
        if isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("scope") == "agent"  # pyright: ignore[reportUnknownMemberType]
        and field.json_schema_extra.get("writable") is not False  # pyright: ignore[reportUnknownMemberType]
    ]
    assert not violations, (
        f"Agent-scope fields with writable != False: {violations}. "
        "Agent-scope fields are per-process and must never be config-writable."
    )


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "Infinity"])
def test_update_quiesce_timeout_rejects_non_finite(
    bad: str,
) -> None:
    """The quiesce window must be finite at the parsing layer: an infinite
    value would make the force-reap backstop's deadline never fire and a
    rollout hang forever. `allow_inf_nan=False` rejects NaN/±Inf as they
    arrive from the .env surface (string -> float), matching the #686
    finite-value standard."""
    from pydantic import ValidationError

    from shared.config.gateway import GatewaySettings

    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"update_quiesce_timeout_seconds": bad})


def test_update_quiesce_timeout_requires_positive_finite_values() -> None:
    from pydantic import ValidationError

    from shared.config.gateway import GatewaySettings

    assert (
        GatewaySettings.model_validate(
            {"update_quiesce_timeout_seconds": "5"}
        ).update_quiesce_timeout_seconds
        == 5.0
    )
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"update_quiesce_timeout_seconds": "0"})


def test_cluster_secret_validator_allows_url_safe_and_empty() -> None:
    """Empty (off / default) and URL-safe tokens pass — they are safe in URLs,
    redis.conf, and a bearer header."""
    assert config.DataPlaneSettings._validate_cluster_secret("") == ""
    assert config.DataPlaneSettings._validate_cluster_secret("Abc-123._~") == "Abc-123._~"


def test_cluster_secret_validator_rejects_unsafe_chars() -> None:
    """A secret with whitespace / newline / a redis-config metachar is rejected —
    it could inject a redis directive or break a data-plane URL."""
    for bad in ("has space", "new\nline", "hash#tag", "tab\tchar", "semi;colon"):
        with pytest.raises(ValueError):
            config.DataPlaneSettings._validate_cluster_secret(bad)


def test_sdk_code_reminder_cadence_config_contract() -> None:
    """The code-category reminder cadence is a live per-agent enum whose
    default preserves the existing once-per-context-window behavior."""
    from shared.config import FIELD_INFOS, get_config_metadata

    field = FIELD_INFOS["sdk_code_reminder_cadence"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default == "once_per_compaction"
    assert field.alias == "AVA_SDK_CODE_REMINDER_CADENCE"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"
    meta = next(m for m in get_config_metadata() if m.name == "sdk_code_reminder_cadence")
    assert meta.choices == ["once_per_compaction", "every_time"]


def test_sdk_nameerror_hint_enabled_config_contract() -> None:
    """The assumed-persistence NameError hint is enabled by default and can be
    adjusted per agent without changing the `.env` naming surface."""
    from shared.config import FIELD_INFOS

    field = FIELD_INFOS["sdk_nameerror_hint_enabled"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default is True
    assert field.alias == "AVA_SDK_NAMEERROR_HINT_ENABLED"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"


def test_llm_model_code_default_is_deepseek_vision_experiment() -> None:
    """An unset AVA_MODEL must fall back to the cluster's vision-capable model."""
    from shared.config.lm import LmSettings

    assert LmSettings.model_fields["llm_model"].default == "deepseek-v4-flash-vision-exp"


# --- agent_communication_style: enum + legacy-boolean alias ---

_STYLE_ENV = (
    "AVA_AGENT_COMMUNICATION_STYLE",
    "AVA_SYSTEM_PROMPT_PROGRESS",
    "AVA_PROMPT_PROGRESS",
)


def _style_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> str | None:
    """Resolve agent_communication_style from a clean env plus `env`.

    Constructs AgentSettings rather than reading the `settings` singleton: that
    is built at import, so a later setenv never reaches it. Every style alias is
    cleared first so an ambient `.env` cannot decide the outcome.
    """
    from shared.config.agent import AgentSettings

    for key in _STYLE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AgentSettings().agent_communication_style


def test_communication_style_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env at all leaves the None sentinel (unset -> per-model resolution),
    whose shared floor is 'off' by user ruling (2026-08-22), so the section is
    omitted unless explicitly enabled."""
    from shared.lm.registry import DEFAULT_TUNING

    assert _style_from_env(monkeypatch) is None
    assert DEFAULT_TUNING.agent_communication_style == "off"


@pytest.mark.parametrize("style", ["oriented", "concise", "silent", "off"])
def test_communication_style_accepts_each_member(monkeypatch: pytest.MonkeyPatch, style) -> None:
    assert _style_from_env(monkeypatch, AVA_AGENT_COMMUNICATION_STYLE=style) == style  # pyright: ignore[reportUnknownArgumentType]


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("false", "silent"),
        ("0", "silent"),
        ("no", "silent"),
        ("FALSE", "silent"),
        ("true", "oriented"),
        ("1", "oriented"),
        ("on", "oriented"),
    ],
)
def test_legacy_progress_boolean_maps_to_a_style(
    monkeypatch: pytest.MonkeyPatch,
    legacy,
    expected,
) -> None:
    """A `.env` written before the enum existed keeps meaning what it meant:
    the old off-state (no narration section) is `silent`, the old default is
    `oriented`. Both legacy spellings of the alias are honored."""
    assert _style_from_env(monkeypatch, AVA_SYSTEM_PROMPT_PROGRESS=legacy) == expected  # pyright: ignore[reportUnknownArgumentType]
    assert _style_from_env(monkeypatch, AVA_PROMPT_PROGRESS=legacy) == expected  # pyright: ignore[reportUnknownArgumentType]


def test_legacy_progress_alias_off_reaches_the_new_off_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'off' is excluded from the legacy false-spellings set: it is now the
    enum's own 'off' member (omit the section entirely), a stronger meaning
    than the 'silent' this alias used to produce for it. Even coming in
    through the retired alias, it passes through unchanged."""
    assert _style_from_env(monkeypatch, AVA_SYSTEM_PROMPT_PROGRESS="off") == "off"
    assert _style_from_env(monkeypatch, AVA_PROMPT_PROGRESS="off") == "off"


def test_new_env_var_wins_over_legacy_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    """AliasChoices order: the enum name is checked first, so a stale legacy
    boolean left in `.env` cannot override an explicit style."""
    style = _style_from_env(
        monkeypatch,
        AVA_AGENT_COMMUNICATION_STYLE="silent",
        AVA_SYSTEM_PROMPT_PROGRESS="true",
    )
    assert style == "silent"


@pytest.mark.parametrize("bad", ["loud", "", "verbose", "2"])
def test_unknown_communication_style_fails_fast(monkeypatch: pytest.MonkeyPatch, bad) -> None:
    """Only the documented boolean spellings are translated; anything else
    reaches Literal validation and raises rather than being guessed at."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _style_from_env(monkeypatch, AVA_AGENT_COMMUNICATION_STYLE=bad)  # pyright: ignore[reportUnknownArgumentType]


def test_communication_style_is_a_per_agent_enum() -> None:
    """The field is overridable per agent (spawn config_overlay) and surfaces to
    the config panel as an enum with its three members."""
    from shared.config import FIELD_INFOS

    field = FIELD_INFOS["agent_communication_style"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert extra["per_agent"] is True
    assert extra["writable"] is True
    assert extra["scope"] == "cluster-default"
    assert extra["restart_required"] == "agent"
    assert config.field_alias_map()["agent_communication_style"] == "AVA_AGENT_COMMUNICATION_STYLE"


_HELPER_ENV = (
    "AVA_PERMISSIONS_HELPER_PORT",
    "AVA_NATIVE_HELPER_PORT",
    "AVA_PERMISSIONS_HELPER_ENABLED",
    "AVA_NATIVE_HELPER_ENABLED",
)


def _helper_port_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> int:
    """Resolve permissions_helper_port from a clean env plus `env` (same pattern
    as `_style_from_env`: the settings singleton is built at import, so a later
    setenv never reaches it; every alias is cleared first so an ambient .env
    cannot decide the outcome)."""
    from shared.config.services import ServiceSettings

    for key in _HELPER_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ServiceSettings().permissions_helper_port


def test_permissions_helper_port_defaults_to_9223(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _helper_port_from_env(monkeypatch) == 9223


def test_permissions_helper_port_reads_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _helper_port_from_env(monkeypatch, AVA_PERMISSIONS_HELPER_PORT="18010") == 18010


def test_permissions_helper_port_falls_back_to_legacy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster born before the rename carries AVA_NATIVE_HELPER_PORT in its
    .env; the new field must still resolve its allocated port from it."""
    assert _helper_port_from_env(monkeypatch, AVA_NATIVE_HELPER_PORT="18010") == 18010


def test_permissions_helper_port_new_key_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        _helper_port_from_env(
            monkeypatch, AVA_PERMISSIONS_HELPER_PORT="18010", AVA_NATIVE_HELPER_PORT="11111"
        )
        == 18010
    )


def test_permissions_helper_serialization_alias_is_the_new_key() -> None:
    """The config panel writes the NEW key (serialization alias wins), so a PUT
    lands on the canonical name, never resurrecting the legacy one."""
    from shared.config import field_alias

    assert field_alias("permissions_helper_port") == "AVA_PERMISSIONS_HELPER_PORT"
    assert field_alias("permissions_helper_enabled") == "AVA_PERMISSIONS_HELPER_ENABLED"


# --- delay-list env field (AVA_IM_SEND_RETRY_DELAYS; AVA_EMBED_RETRY_DELAYS
# removed in R2-D — the embedder's retry policy is a shared.resilience Policy
# constant now, per design evaluation-record #14) ---


def _im_send_delays_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> list[float]:
    """Construct ServiceSettings from a clean env plus `env` (the singleton is
    built at import, so a later setenv never reaches it)."""
    from shared.config.services import ServiceSettings

    # setenv/delenv via loop variables — the direct literal spelling is
    # linted (settings is a module-load singleton), the indirect one
    # reaches the fresh ServiceSettings() construction below.
    for key in ("AVA_IM_SEND_RETRY_DELAYS",):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ServiceSettings().im_send_retry_delays


def test_delay_lists_accept_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A comma-separated env value parses into the delay list — the spelling a
    .env operator would naturally write (task #698 G8; regression: NoDecode
    handed the raw string to list[float] and Settings construction crashed,
    killing every spawned agent in e2e)."""
    im = _im_send_delays_from_env(monkeypatch, AVA_IM_SEND_RETRY_DELAYS="0.5,1")
    assert im == [0.5, 1.0]


def test_delay_lists_accept_json_array_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON-array spelling pydantic-settings would natively decode also
    works."""
    im = _im_send_delays_from_env(monkeypatch, AVA_IM_SEND_RETRY_DELAYS="[2.0, 4.0, 8.0]")
    assert im == [2.0, 4.0, 8.0]


def test_delay_lists_default_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    im = _im_send_delays_from_env(monkeypatch)
    assert im == [2.0, 4.0, 8.0, 16.0, 32.0]


# --- AVA_IM_DISABLED_ADAPTERS (Task #855; P0 fix: NoDecode list field needs
# the same before-validator treatment or any env value crashes settings load) ---


def _disabled_adapters_from_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None = None
) -> list[str]:
    from shared.config.services import ServiceSettings

    # setenv via a loop variable — the direct literal spelling is linted
    # (settings is a module-load singleton), the indirect one reaches the
    # fresh ServiceSettings() construction below, same as the delay-list
    # tests.
    for key in ("AVA_IM_DISABLED_ADAPTERS",):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    s = ServiceSettings()
    return s.im_disabled_adapters


def test_disabled_adapters_accept_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The natural .env spelling — "weixin,feishu" — parses into the list."""
    assert _disabled_adapters_from_env(monkeypatch, "weixin,feishu") == ["weixin", "feishu"]


def test_disabled_adapters_accept_json_array_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON-array spelling works too."""
    assert _disabled_adapters_from_env(monkeypatch, '["weixin", "feishu"]') == [
        "weixin",
        "feishu",
    ]


def test_disabled_adapters_accept_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value must not crash — it means "nothing disabled"."""
    assert _disabled_adapters_from_env(monkeypatch, "") == []


def test_disabled_adapters_default_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env at all -> default empty (all adapters load)."""
    assert _disabled_adapters_from_env(monkeypatch) == []


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


def test_service_reads_stay_full_when_singleton_domain_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap / current_field_values / flat_dump serve the same complete
    payload with a gateway-profile-limited singleton as without one."""
    from shared import config

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
    from shared import config

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
    from shared import config

    return config.Settings(profile=profile)


def test_gateway_profile_excludes_agent_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway profile: sandbox/agent/web are NOT constructed — access raises an
    actionable AttributeError; lm/telegram/feishu ARE (real gateway-side reads)."""
    from shared import config

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
    """Agent profile constructs its matrix domains; daemon included (ava_fleet
    plugin's in-agent task_maintenance reads it); alerts/telegram/feishu excluded."""
    from shared import config

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


def test_runner_profile_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Support daemons include browser sandbox, telemetry, and backup settings.

    Agent execution settings belong to the agent-host's separate agent profile.
    """
    from shared import config

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
        "physical_backup",
    ):
        assert s.has_domain(domain), domain
    for domain in ("agent", "web", "alerts", "telegram", "feishu"):
        assert not s.has_domain(domain), domain
        with pytest.raises(AttributeError):
            getattr(s, domain)


def test_settings_reads_env_profile_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain Settings() reads AVA_PROCESS_PROFILE from the environment; with
    the marker absent it constructs every domain (unchanged behavior)."""
    from shared import config

    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings()
    assert not s.has_domain("agent")
    monkeypatch.delenv(config.AVA_PROCESS_PROFILE_ENV, raising=False)
    s2 = config.Settings()
    assert s2.has_domain("agent") and s2.has_domain("sandbox") and s2.has_domain("web")


def test_unknown_profile_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown profile name is a launcher bug — fail at construction."""
    from shared import config

    with pytest.raises(ValueError):
        config.Settings(profile="bogus")


def test_explicit_none_profile_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """profile=None (the config-service read paths, D5) builds every domain even
    when the environment carries a profile marker."""
    from shared import config

    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "gateway")
    s = config.Settings(profile=None)
    assert s.has_domain("agent") and s.has_domain("sandbox") and s.has_domain("web")


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
    from shared import config

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


# ─── legacy inverted aliases resolve with correct semantics ───


def test_gateway_session_ttl_defaults_to_one_day() -> None:
    from shared.config.gateway import GatewaySettings

    assert GatewaySettings().session_ttl_seconds == 24 * 3600


def test_timeline_compact_history_config_contract() -> None:
    from shared.config import field_alias_map
    from shared.config.gateway import GatewaySettings

    field = GatewaySettings.model_fields["timeline_compact_history"]
    extra = field.json_schema_extra

    assert GatewaySettings().timeline_compact_history == 1
    assert field_alias_map()["timeline_compact_history"] == "AVA_TIMELINE_COMPACT_HISTORY"
    assert isinstance(extra, dict)
    assert extra["restart_required"] == "gateway"
    assert extra["writable"] is True
    assert extra["scope"] == "cluster-pinned"
    assert extra["per_agent"] is False
    assert field.description == (
        "Number of compact-history segments the timeline may load backward: "
        "0 disables compact history, -1 allows all retained segments, and N "
        "allows the newest N segments."
    )


def test_skip_auth_alias_inverts_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_SKIP_AUTH means "skip auth" — true must resolve to auth DISABLED.

    The inversion happens in dotenv_boot's boot-time translation (the single
    .env load entry every process imports); the test drives the same chain:
    translate env -> construct Settings."""
    from shared.config.gateway import GatewaySettings
    from shared.dotenv_boot import _translate_legacy_skip_aliases

    monkeypatch.setitem(os.environ, "AVA_SKIP_AUTH", "true")
    monkeypatch.setitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", "true")
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)
    _translate_legacy_skip_aliases()
    assert GatewaySettings().auth_middleware_enabled is False

    # Second phase: drop the translated canonical key (a real boot has only one
    # value), then translate the other legacy spelling.
    monkeypatch.setitem(os.environ, "AVA_SKIP_AUTH", "false")
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)
    _translate_legacy_skip_aliases()
    assert GatewaySettings().auth_middleware_enabled is True


def test_skip_security_scan_alias_inverts_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_SKIP_SECURITY_SCAN means "skip the scan" — true must resolve to scan
    DISABLED (translation chain as above)."""
    from shared.config.agent_eval import AgentEvalSettings
    from shared.dotenv_boot import _translate_legacy_skip_aliases

    monkeypatch.setitem(os.environ, "AVA_SKIP_SECURITY_SCAN", "true")
    monkeypatch.setitem(os.environ, "AVA_SECURITY_SCAN_ENABLED", "true")
    monkeypatch.delitem(os.environ, "AVA_SECURITY_SCAN_ENABLED", raising=False)
    _translate_legacy_skip_aliases()
    assert AgentEvalSettings().security_scan_enabled is False

    monkeypatch.setitem(os.environ, "AVA_SKIP_SECURITY_SCAN", "false")
    monkeypatch.delitem(os.environ, "AVA_SECURITY_SCAN_ENABLED", raising=False)
    _translate_legacy_skip_aliases()
    assert AgentEvalSettings().security_scan_enabled is True


@pytest.mark.parametrize("initial", [None, "true"])
@pytest.mark.parametrize("family", ["auth", "scan"])
def test_skip_alias_test_restores_canonical_environment(
    monkeypatch: pytest.MonkeyPatch, initial: str | None, family: str
) -> None:
    """A translator's direct env writes must not escape either test phase."""
    key = "AVA_AUTH_MIDDLEWARE_ENABLED" if family == "auth" else "AVA_SECURITY_SCAN_ENABLED"
    if initial is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, initial)
    with pytest.MonkeyPatch.context() as isolated:
        if family == "auth":
            test_skip_auth_alias_inverts_value(isolated)
        else:
            test_skip_security_scan_alias_inverts_value(isolated)
    assert os.environ.get(key) == initial


def test_eval_isolation_env_aliases_parse(tmp_path: Path) -> None:
    """The child process receives the aliases before its Settings singleton loads."""
    proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                from shared.config.agent_eval import AgentEvalSettings
                config = AgentEvalSettings()
                assert config.eval_isolation is True
                assert config.eval_network_allowlist == ["web", "understand"]
                print("ok")
            """),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AVA_HOME": str(tmp_path),
            "AVA_CONFIG_FETCH": "skip",
            "AVA_EVAL_ISOLATION": "true",
            "AVA_EVAL_NETWORK_ALLOWLIST": "web, understand",
        },
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_skip_aliases_canonical_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both keys present -> the canonical key is authoritative (AliasChoices
    order), never the translated legacy value."""
    from shared.config.gateway import GatewaySettings
    from shared.dotenv_boot import _translate_legacy_skip_aliases

    monkeypatch.setitem(os.environ, "AVA_SKIP_AUTH", "true")
    monkeypatch.setitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", "true")
    _translate_legacy_skip_aliases()
    assert GatewaySettings().auth_middleware_enabled is True


def test_skip_translation_leaves_unparseable_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy value pydantic cannot parse is left untouched — the translation
    never guesses; Settings raises its usual ValidationError."""
    import os

    from shared.dotenv_boot import _translate_legacy_skip_aliases

    monkeypatch.setenv("AVA_SKIP_AUTH", "banana")
    monkeypatch.delitem(os.environ, "AVA_AUTH_MIDDLEWARE_ENABLED", raising=False)
    _translate_legacy_skip_aliases()
    assert "AVA_AUTH_MIDDLEWARE_ENABLED" not in os.environ


# ─── current_field_values warns on an undecodable .env value ───


def test_current_field_values_warns_on_undecodable_env_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A .env value the field annotation cannot decode falls back to the boot
    value WITH a warning naming the key — never silently: the bad line stays in
    the file and the next process start's Settings construction will fail on it,
    so the operator must hear about it at panel-read time (audit round-2
    config.md P2)."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": "banana"}, set())

    val = config.current_field_values()["trace_enabled"]

    assert val is True  # boot-time fallback (the field default)
    assert len(rec.warnings) == 1
    assert "AVA_TRACE_ENABLED" in rec.warnings[0]
    assert "banana" not in rec.warnings[0]


def test_current_field_values_warns_on_bad_nodecode_list_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A genuinely undecodable NoDecode list value (non-numeric delays) warns
    and falls back to the boot-time value — never a wrong-typed string split
    (the old fallback served ["banana", "apple"] for a list[float] field)."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_send_retry_delays": "banana,apple"}, set())

    values = config.current_field_values()

    assert values["im_send_retry_delays"] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert len(rec.warnings) == 1
    assert "AVA_IM_SEND_RETRY_DELAYS" in rec.warnings[0]
    assert "banana,apple" not in rec.warnings[0]


def test_current_field_values_isolates_bad_env_from_good_file_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """QA #1090 repro: a good comma-list FILE value must decode even when
    ANOTHER field of the same domain carries a bad ENV value (absent from the
    file). The decode payload covers every field, so model_validate never reads
    os.environ — the old retry did, dropped the good file value, and served
    the boot value instead."""
    from shared import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": ["weixin", "feishu"]}, set())
    # setitem, not setenv: the lint bans setenv on Settings aliases (the
    # singleton never re-reads env); this plants the bad value for the decode
    # path only.
    monkeypatch.setitem(os.environ, "AVA_IM_SEND_RETRY_DELAYS", "banana")

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


# ─── AVA_TIMEZONE fails fast at Settings construction ───


def test_timezone_validated_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad AVA_TIMEZONE crashed the first inbound turn of every agent
    (ZoneInfo raises in now_timestamp with no try/except); the validator moves
    the failure to Settings construction, where it is loud and immediate."""
    from pydantic import ValidationError

    from shared.config.general import GeneralSettings

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "Not/A_Timezone")
    with pytest.raises(ValidationError, match="not a valid IANA timezone"):
        GeneralSettings()

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "Asia/Shanghai")
    assert GeneralSettings().timezone == "Asia/Shanghai"


def _capture_loguru_warnings() -> tuple[list[str], int]:
    """A loguru sink collecting WARNING+ records; returns (records, sink_id).

    The timezone-default warning fires during the Settings singleton build,
    before any stdlib -> loguru bridge exists, so it goes to loguru directly —
    caplog (stdlib) cannot see it."""
    from loguru import logger

    records: list[str] = []

    class _Sink:
        def write(self, message: str) -> None:
            records.append(message)

    return records, logger.add(_Sink(), level="WARNING", format="{message}")


def test_timezone_default_warns_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing AVA_TIMEZONE must not drift silently onto the
    America/Los_Angeles default: a schedule runner with that default fires cron
    jobs at PT midnight instead of the cluster's midnight (2026-08-21 incident,
    schedule #3). The warning names the missing key so the operator fixes the
    cluster .env instead of discovering the wrong fire time."""
    from loguru import logger

    from shared.config.general import GeneralSettings

    monkeypatch.delitem(os.environ, "AVA_TIMEZONE", raising=False)
    records, sink_id = _capture_loguru_warnings()
    try:
        GeneralSettings()
    finally:
        logger.remove(sink_id)
    assert any("AVA_TIMEZONE is not set" in r and "America/Los_Angeles" in r for r in records)


def test_timezone_explicit_value_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit AVA_TIMEZONE (any zone, including PT) is a deliberate
    choice — no warning."""
    from loguru import logger

    from shared.config.general import GeneralSettings

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "America/Los_Angeles")
    records, sink_id = _capture_loguru_warnings()
    try:
        GeneralSettings()
    finally:
        logger.remove(sink_id)
    assert not any("AVA_TIMEZONE is not set" in r for r in records)


# ─── frontend config-group map alignment (ui/web/src/app/control/_config_groups.ts) ───


def test_frontend_config_group_keys_match_backend_aliases() -> None:
    """Every env var in the frontend's GROUP_ENV_VARS display map must be a real
    backend field alias — a dead key silently renders nothing and the field it
    meant falls into a default bucket, possibly the WRONG display group
    (AVA_SYSTEM_PROMPT_PROGRESS was retired for AVA_AGENT_COMMUNICATION_STYLE,
    which rendered under config-exec instead of config-prompts; and
    AVA_AGENT_AUTONOMOUS_PUSH never existed — audit round-2 config.md P2)."""
    import re
    from pathlib import Path

    from shared.config import field_alias_map

    groups_ts = (
        Path(__file__).resolve().parent.parent.parent
        / "ui"
        / "web"
        / "src"
        / "app"
        / "control"
        / "_config_groups.ts"
    )
    assert groups_ts.exists(), f"missing frontend map: {groups_ts}"
    src = groups_ts.read_text()
    block_start = src.index("export const GROUP_ENV_VARS")
    block_end = src.index("};", block_start) + 2
    keys = re.findall(r'"([A-Z][A-Z0-9_]*)"', src[block_start:block_end])
    assert len(keys) > 100, f"parse looks wrong — only {len(keys)} keys extracted"

    aliases = set(field_alias_map().values())
    dead = [k for k in keys if k not in aliases]
    assert not dead, (
        f"frontend GROUP_ENV_VARS keys with no backend field: {dead} — "
        f"remove them or fix the spelling (a dead key renders nothing and the "
        f"real field falls into a default bucket)"
    )


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
    unapplied until a manual kickstart (Task #2224). milvus_uri is the same
    bug class: memory_indexer's milvus backend, the milvus healthcheck and
    the gateway search path all read it.
    """
    from shared.config.profiles import PROCESS_PROFILES
    from shared.config.services import ServiceSettings

    # The owning domain must be in the gateway profile for "gateway" to be the
    # honest value — the consumption matrix, kept in sync with the profile set.
    assert "services" in PROCESS_PROFILES["gateway"]

    for name in ("milvus_uri", "memory_search_backend"):
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
    from shared.config.feishu import FeishuSettings
    from shared.config.profiles import PROCESS_PROFILES
    from shared.config.services import ServiceSettings
    from shared.config.telegram import TelegramSettings

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

    from shared import config_registry
    from shared.config._base import EnvSettings

    class Bad(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="AVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "gatewat"},
        )

    monkeypatch.setattr(
        config_registry,
        "_DOMAIN_MODELS",
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

    from shared import config_registry
    from shared.config._base import EnvSettings

    class WrongKind(EnvSettings):
        synthetic_knob: int = Field(
            default=1,
            alias="AVA_SYNTHETIC_KNOB",
            json_schema_extra={"scope": "cluster-pinned", "restart_required": "agent"},
        )

    monkeypatch.setattr(
        config_registry,
        "_DOMAIN_MODELS",
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

    from shared.config import _FIELDS
    from shared.config.profiles import PROCESS_PROFILES
    from shared.config_registry import _ALLOWED_RESTART_REQUIRED, _RESTART_REQUIRED_PROFILE

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
