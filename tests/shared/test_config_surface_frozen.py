"""The decomposition must not move the wire: env aliases, the flat metadata
surface, and the per-agent overlay payload shape are all frozen. These pin the
round-trip explicitly so a future sub-model edit that renamed an alias, nested a
key, or reshaped the restart_completed snapshot fails loudly here.
"""

from __future__ import annotations

from shared import config

# Critical aliases whose exact spelling IS the user-facing `.env` contract — a
# rename here silently breaks every deployed `.env`, enroll materialization, and
# the config PUT. Spot-check across domains.
_FROZEN_ALIASES = {
    "db_url": "AVA_DB_URL",
    "redis_url": "AVA_REDIS_URL",
    "events_channel": "AVA_EVENTS_CHANNEL",
    "cluster_secret": "AVA_CLUSTER_SECRET",
    "llm_model": "AVA_MODEL",
    "labeler_model": "AVA_LABELER_MODEL",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "exec_timeout_seconds": "AVA_EXEC_TIMEOUT_SECONDS",
    "ava_home": "AVA_HOME",
    "telegram_bot_token": "AVA_TELEGRAM_BOT_TOKEN",
    "skills_to_inject_into_system_prompt": "AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT",
    "gateway_port": "AVA_GATEWAY_PORT",
}


def test_env_alias_surface_is_flat_and_frozen() -> None:
    """Every field's `.env` alias is unchanged by the split: the field->alias map
    still covers exactly the field set, and the critical aliases (one per domain)
    keep their exact spelling — the user-facing `.env` contract is untouched."""
    amap = config.field_alias_map()
    assert set(amap) == config.field_names()

    # Critical aliases across every domain are byte-identical to before the split.
    for name, alias in _FROZEN_ALIASES.items():
        assert amap[name] == alias, f"{name}: alias drifted to {amap[name]!r}"
    # The split is invisible to `.env`: the flat_dump / snapshot tests below prove
    # no domain KEY (lm / data_plane / …) reaches the value surface either.


def test_flat_dump_keys_are_leaf_names_no_domain_nesting() -> None:
    """`flat_dump()` (backing the config-overlay snapshot) is keyed by leaf field
    NAME with no `lm`/`data_plane`/... domain nesting — the shape the old flat
    `settings.model_dump()` produced."""
    dump = config.flat_dump(mode="json")
    assert set(dump) == config.field_names()

    domain_attrs = {a for a, _label, _m, _cap in config._DOMAIN_MODELS}
    assert not (domain_attrs & set(dump)), "a domain key leaked into the flat dump"
    # spot-check fields from different domains sit at the top level
    for name in ("db_url", "llm_model", "exec_timeout_seconds", "gateway_port"):
        assert name in dump


def test_effective_config_snapshot_is_flat_framework_keys() -> None:
    """The `restart_completed` payload carries framework fields at the top
    level by name (no domain nesting) — the event-trail shape is byte-stable across
    the decomposition. Plugin fields are the only prefixed keys (`<plugin>.<field>`).

    Sensitive fields (`json_schema_extra={"sensitive": True}` — db_url, the
    lm api keys, ...) are deliberately excluded: the snapshot is stored as
    plain JSON on every restart_completed row (2026-08-08 audit, P2-7), so a
    sensitive value must not get a second plaintext copy there."""
    from shared.plugin_config_registry import (
        _framework_field_is_sensitive,
        clear_plugin_configs,
        effective_config_snapshot,
    )

    clear_plugin_configs()  # no plugins bound -> pure framework snapshot
    try:
        snap = effective_config_snapshot()
    finally:
        clear_plugin_configs()

    assert {
        name for name in config.field_names() if not _framework_field_is_sensitive(name)
    } <= set(snap)
    assert not any(_framework_field_is_sensitive(name) for name in snap)
    # a non-sensitive data_plane field is present flat, not as `data_plane.<field>`
    assert "gateway_url" in snap and "data_plane.gateway_url" not in snap
    assert "data_plane" not in snap


def test_bootstrap_payload_keys_are_modeled_or_enabled_plugin_aliases() -> None:
    """Bootstrap serves Settings aliases plus declared enabled-provider keys."""
    from shared.env_registry import _enabled_provider_key_envs

    served = config.bootstrap_config_values()
    valid = {config.field_alias(name) for name in config.BOOTSTRAP_FIELDS}
    valid |= _enabled_provider_key_envs()
    assert set(served) <= valid


def test_overlay_flat_key_resolves_to_owning_submodel() -> None:
    """The agent-facing per-agent overlay contract is a FLAT field-name key. It must
    resolve to (and set on) the owning sub-model — the flat->nested bridge the
    `config_overlay={"llm_model": ...}` SDK surface depends on."""
    assert config.field_domain("llm_model") == "lm"
    assert config.field_domain("db_url") == "data_plane"
    assert config.field_domain("exec_timeout_seconds") == "sandbox"
    # get_field / set_field round-trip on a flat name hits the sub-model
    original = config.get_field("llm_model")
    try:
        config.set_field("llm_model", "claude-opus-4-7")
        assert config.settings.lm.llm_model == "claude-opus-4-7"
        assert config.get_field("llm_model") == "claude-opus-4-7"
    finally:
        config.set_field("llm_model", original)
