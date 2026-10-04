"""Settings registry contracts: scope and capability declarations, read-only identity fields, bootstrap derivation and the remote-writable surface; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import pytest

from base import config


def test_openai_api_key_field_exists_and_is_per_secret():
    from base.config import FIELD_INFOS, field_domain

    field = FIELD_INFOS["openai_api_key"]
    assert field.alias == "OPENAI_API_KEY"
    extra = field.json_schema_extra
    assert isinstance(extra, dict)  # narrow JsonDict | Callable | None for subscript
    assert extra["sensitive"] is True
    # A provider key is owned by the LLM domain (group is derived from the sub-model).
    assert field_domain("openai_api_key") == "lm"


def test_retired_runner_selector_is_not_configurable() -> None:
    """Only the agent host runs agents; no UI or overlay can select the old runner."""
    from base.config import FIELD_INFOS, get_config_metadata

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
    from base.config import FIELD_INFOS

    for name in (
        "skill_match_enabled",
        "skill_match_top_k",
        "skill_match_min_score",
        "skill_match_budget_ms",
    ):
        assert name not in FIELD_INFOS, f"{name} must not be a registered settings field"


def test_every_field_declares_valid_scope() -> None:
    """Every Settings field must declare an ownership scope in json_schema_extra."""
    from base.config import FIELD_INFOS

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
    from base.config import get_config_metadata
    from base.host.env.config_registry import _ALLOWED_CAPABILITIES

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
    from base.config import get_config_metadata

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
        "trace_retention_days": "common",  # observability domain default
    }
    for name, want in expected.items():
        assert cap[name] == want, f"{name}: capability drifted to {cap[name]!r} (want {want!r})"


def test_build_registry_rejects_bad_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd capability can never reach prod boot. `_build_registry` runs at
    import and raises on a bad resolved capability (a field override or, here, a
    domain default), so the whole process fails fast rather than silently
    mis-grouping — the same seal `scope` has. Pin the raise directly (the
    all-fields-valid test above only proves the current tree is clean)."""
    from base.host.env import config_registry

    # The registry builds lazily on first use and memoizes; patch the module
    # (not the config re-export) and clear the cache so the typo is exercised.
    monkeypatch.setattr(
        config_registry,
        "DOMAIN_MODELS",
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
    from base.config import FIELD_INFOS

    for name in ("db_url", "redis_url"):
        extra = FIELD_INFOS[name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["writable"] is False, f"{name} must be writable=False"


def test_host_identity_fields_are_read_only() -> None:
    """Host identity / connection / infra fields are not panel-writable — they're set
    by the first `ava start`, not the runtime config panel. (The config
    write path enforces this too; the metadata must agree so the UI shows read-only.)"""
    from base.config import FIELD_INFOS

    for name in (
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
    from base.config import BOOTSTRAP_FIELDS, FIELD_INFOS

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


def test_offsite_backup_cluster_pinned_fields_are_never_bootstrap_served() -> None:
    """A runner must never receive the off-site destination without its host credentials."""
    from base.config import BOOTSTRAP_FIELDS, FIELD_INFOS

    offsite_cluster_pinned = {
        name
        for name, field in FIELD_INFOS.items()
        if name.startswith("backup_offsite_")
        and isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("scope") == "cluster-pinned"  # pyright: ignore[reportUnknownMemberType]
    }

    assert offsite_cluster_pinned
    assert not (set(BOOTSTRAP_FIELDS) & offsite_cluster_pinned)


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_distributes_a_behavior_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default agent-behavior knob is now distributed to agent-runners."""
    from base import config as cfg
    from base.host.env import runtime_config

    # .env file may carry a stale value from another test; bypass it so the
    # monkeypatched settings value is the only source.
    monkeypatch.setattr(
        runtime_config,
        "read_env_aliases",
        lambda: {"AVA_DB_URL": str(cfg.settings.data_plane.db_url)},
    )
    monkeypatch.setattr(cfg.settings.sandbox, "exec_timeout_seconds", 123.0)
    values = cfg.bootstrap_config_values()
    assert values["AVA_EXEC_TIMEOUT_SECONDS"] == "123.0"


def test_cluster_default_iff_per_agent() -> None:
    """scope=cluster-default fields must be marked per_agent=True (one-way, not iff)."""
    from base.config import FIELD_INFOS

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
    from base.config import FIELD_INFOS

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
        "browser_enabled",
        "browser_reach_failure_threshold",
        "browser_reach_probe_interval_s",
        "browser_reach_timeout_s",
        "chrome_binary",
        "computer_use_lease_s",
        "computer_use_loop_stall_s",
        "computer_use_queue_timeout_s",
        "computer_use_shutdown_drain_s",
        "computer_use_session_idle_s",
        "delivery_outbox_enabled",
        "delivery_watchdog_enabled",
        "exec_request_bounded_quarantine_enabled",
        "heartbeat_enabled",
        "host_abort_reconcile_enabled",
        "host_turn_reconcile_enabled",
        "hosted_crash_recovery_wake_enabled",
        "hosted_recrash_prompt_reap_enabled",
        "inbound_reconcile_boundary_scan_limit",
        "inbound_reconcile_clock_pad_seconds",
        "inbound_reconcile_window_row_cap",
        "machine_description",
        "permissions_helper_enabled",
        "permissions_helper_spawn",
        "ops_concurrency",
        "task_maintenance_enabled",
        "venv_probe_failure_threshold",
    }
)


def test_remote_writable_allowlist_is_exact() -> None:
    """Exactly the allowlist fields are remote_writable=True; all others are False."""
    from base.config import FIELD_INFOS

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
    from base.config import FIELD_INFOS

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
    """Every agent-scope field must have writable=False: no override store exists
    behind a config PUT for process-startup env vars, so the writable gate keeps
    them out."""
    from base.config import FIELD_INFOS

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
