"""Tests for Plugin config registration + disk image (`agent/config.py`).

Covers:
- register_plugin_config: PluginContext required, duplicate registration raise, non-BaseModel raise
- bind_from_disk: auto-write default (disk missing), instantiation OK; schema drift raise
- merge_disk_image_schema: new fields fill default, removed fields dropped, unchanged no-op
- is_per_agent_field: json_schema_extra={"per_agent": True} recognition
- ava._settings.plugins.<n> attribute access wrong name raise + list known plugins
"""

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from shared.plugin_config_registry import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    DuplicateRegistration,
    InvalidConfigOverlay,
    NoPluginContext,
    SchemaDriftError,
    apply_config_overlay,
    bind_from_disk,
    clear_plugin_configs,
    disk_image_path,
    effective_config_snapshot,
    get_plugin_config,
    is_per_agent_field,
    merge_disk_image_schema,
    register_plugin_config,
    resolve_overlay_targets,
    validate_config_overlay,
    write_default_disk_image,
)
from shared.plugin_context import PluginContext


class _FixtureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    flag: bool = Field(default=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})


@pytest.fixture
def isolated_registry():
    """Per-test clean registry — avoids cross-test pollution.

    This fixture teardown re-registers to restore initial state
    (note: registration order doesn't matter; zero cross-test impact).
    """
    # Snapshot before
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    clear_plugin_configs()
    yield
    clear_plugin_configs()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


def test_register_requires_plugin_context(isolated_registry):
    """register_plugin_config outside PluginContext → NoPluginContext."""
    with pytest.raises(NoPluginContext, match="PluginContext"):
        register_plugin_config(_FixtureConfig)


def test_register_non_basemodel_raises(isolated_registry):
    class _NotBaseModel:
        pass

    with PluginContext("test_plugin"), pytest.raises(TypeError, match="BaseModel subclass"):
        register_plugin_config(_NotBaseModel)  # type: ignore[arg-type]


def test_register_duplicate_raises(isolated_registry):
    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
        with pytest.raises(DuplicateRegistration, match="test_plugin"):
            register_plugin_config(_FixtureConfig)


def test_bind_from_disk_auto_writes_default_when_missing(isolated_registry, unit_home):
    """disk image missing → bind_from_disk auto-writes default + instantiation OK."""
    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()

    cfg = get_plugin_config("test_plugin", _FixtureConfig)
    assert cfg.flag is True
    assert cfg.marker == ".git"
    # Disk image should have been written
    img = disk_image_path("test_plugin")
    assert img.exists()
    assert json.loads(img.read_text()) == {"flag": True, "marker": ".git"}


def test_bind_from_disk_reads_existing_image(isolated_registry, unit_home):
    """disk image exists and schema matches → bind uses disk values, not cls defaults."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": False, "marker": ".hg"}))  # pyright: ignore[reportUnknownMemberType]

    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()

    cfg = get_plugin_config("test_plugin", _FixtureConfig)
    assert cfg.flag is False
    assert cfg.marker == ".hg"


def test_bind_from_disk_schema_drift_raises(isolated_registry, unit_home):
    """disk image field set doesn't match cls → SchemaDriftError to guide update."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": True, "marker": ".git", "extra_field": 42}))  # pyright: ignore[reportUnknownMemberType]

    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    with pytest.raises(SchemaDriftError, match="schema drift"):
        bind_from_disk()


def test_merge_disk_image_adds_new_field(isolated_registry, unit_home):
    """New field exists in cls but not in disk → merge writes the default value to disk."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(  # pyright: ignore[reportUnknownMemberType]
        json.dumps({"flag": True})
    )  # missing marker  # pyright: ignore[reportUnknownMemberType]

    added, removed = merge_disk_image_schema("test_plugin", _FixtureConfig)
    assert added == {"marker"}
    assert removed == set()
    assert json.loads(img.read_text()) == {"flag": True, "marker": ".git"}  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_merge_disk_image_drops_removed_field(isolated_registry, unit_home):
    """Removed field (disk has, cls doesn't) → drop from disk image + return removed set for CLI display.

    Dropping is key: the field-set strict equality check in bind_from_disk requires disk == cls; keeping leftover fields
    would cause every agent spawn after update to continue hitting SchemaDriftError and become terminated on startup.
    """
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": True, "marker": ".git", "obsolete": "old"}))  # pyright: ignore[reportUnknownMemberType]

    added, removed = merge_disk_image_schema("test_plugin", _FixtureConfig)
    assert added == set()
    assert removed == {"obsolete"}
    # obsolete has been dropped from disk image → field set now matches cls_keys
    data = json.loads(img.read_text())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert data == {"flag": True, "marker": ".git"}


def test_merge_then_bind_resolves_removed_field_drift(isolated_registry, unit_home):
    """Regression: after running merge on removed-field drift (= `ava plugins update` / converge's
    plugin-config-images step), bind_from_disk does not raise SchemaDriftError.

    This is the real production scenario where spawn resulted in terminated on startup (compact_tail_messages
    was removed from schema, leftover disk image). Before the fix, merge kept the leftover fields, bind still crashed.
    """
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": True, "marker": ".git", "obsolete": "old"}))  # pyright: ignore[reportUnknownMemberType]

    merge_disk_image_schema("test_plugin", _FixtureConfig)

    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()  # before fix, would raise SchemaDriftError here

    cfg = get_plugin_config("test_plugin", _FixtureConfig)
    assert cfg.flag is True
    assert cfg.marker == ".git"


def test_merge_disk_image_noop_when_aligned(isolated_registry, unit_home):
    """schema aligned → merge no-op (does not write disk)."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": False, "marker": ".hg"}))  # pyright: ignore[reportUnknownMemberType]
    mtime_before = img.stat().st_mtime  # pyright: ignore[reportUnknownMemberType]

    added, removed = merge_disk_image_schema("test_plugin", _FixtureConfig)
    assert added == set()
    assert removed == set()
    assert img.stat().st_mtime == mtime_before  # pyright: ignore[reportUnknownMemberType]


def test_merge_disk_image_writes_default_when_missing(isolated_registry, unit_home):
    """disk missing → merge treats as first write of defaults, returns all-fields added."""
    added, removed = merge_disk_image_schema("test_plugin", _FixtureConfig)
    assert added == {"flag", "marker"}
    assert removed == set()
    assert disk_image_path("test_plugin").exists()


def test_write_default_disk_image_overwrites(isolated_registry, unit_home):
    """write_default_disk_image always overwrites with cls() default; existing values are not preserved."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": False, "marker": ".old"}))  # pyright: ignore[reportUnknownMemberType]

    write_default_disk_image("test_plugin", _FixtureConfig)
    assert json.loads(img.read_text()) == {"flag": True, "marker": ".git"}  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_is_per_agent_field_metadata(isolated_registry):
    """json_schema_extra={"per_agent": True} → is_per_agent_field True; otherwise False."""
    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)

    assert is_per_agent_field("test_plugin", "marker") is True  # has per_agent metadata
    assert is_per_agent_field("test_plugin", "flag") is False  # no per_agent metadata
    assert is_per_agent_field("test_plugin", "nonexistent") is False
    assert is_per_agent_field("unknown_plugin", "x") is False


# ── overlay (PR-E) ─────────────────────────────────────────────────────────


def _setup_overlayable_plugin():
    """Register a frozen Config with per_agent=True fields, run bind_from_disk.

    Requires the `unit_home` fixture active in the calling test (settings.general.ava_home
    pointing at a per-test tmp dir) so bind_from_disk writes the disk image there,
    not into the shared session home — callers must declare `unit_home`.
    """
    with PluginContext("overlay_test"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()


def test_resolve_overlay_targets_unknown_key_raises(isolated_registry, unit_home):
    _setup_overlayable_plugin()
    with pytest.raises(InvalidConfigOverlay, match="typo"):
        resolve_overlay_targets({"definitely_not_a_field": 1})


def test_resolve_overlay_targets_non_per_agent_raises(isolated_registry, unit_home):
    """`flag` field is not marked per_agent → InvalidConfigOverlay."""
    _setup_overlayable_plugin()
    with pytest.raises(InvalidConfigOverlay, match="per_agent=True"):
        resolve_overlay_targets({"flag": False})


def test_resolve_overlay_targets_per_agent_resolves(isolated_registry, unit_home):
    _setup_overlayable_plugin()
    targets = resolve_overlay_targets({"marker": ".hg"})
    assert targets == {"marker": ("overlay_test", "marker")}


def test_validate_config_overlay_type_error_raises(isolated_registry, unit_home):
    """marker is a str field, passing int triggers Pydantic ValidationError → InvalidConfigOverlay."""
    _setup_overlayable_plugin()
    with pytest.raises(InvalidConfigOverlay, match="type validation"):
        validate_config_overlay({"marker": 123})


def test_validate_config_overlay_unknown_llm_model_raises(isolated_registry, unit_home):
    with pytest.raises(InvalidConfigOverlay, match="not a registered model") as exc_info:
        validate_config_overlay({"llm_model": "deepseek-v4-flash-vision"})

    assert "deepseek-v4-flash-vision-exp" in str(exc_info.value)


def test_validate_config_overlay_registered_llm_model_passes(isolated_registry, unit_home):
    validate_config_overlay({"llm_model": "claude-opus-4-6"})


def test_validate_config_overlay_unknown_reasoning_effort_raises(isolated_registry, unit_home):
    with pytest.raises(InvalidConfigOverlay, match="valid values"):
        validate_config_overlay({"reasoning_effort": "turbo"})


@pytest.mark.parametrize("effort", ["", "high"])
def test_validate_config_overlay_known_reasoning_effort_passes(
    isolated_registry, unit_home, effort: str
):
    validate_config_overlay({"reasoning_effort": effort})


def test_validate_config_overlay_does_not_range_check_plugin_fields(isolated_registry, unit_home):
    _setup_overlayable_plugin()
    validate_config_overlay({"marker": "any string"})


@pytest.mark.parametrize("field", ["llm_model", "memory_recall_filter_model"])
def test_validate_config_overlay_unknown_model_field_raises(
    isolated_registry, unit_home, field: str
):
    """Every model-name overlay field rejects an unregistered id (same failure
    class as the llm_model incident — an unregistered memory filter model would
    crash the agent in the before_llm hook via build_chat_model)."""
    with pytest.raises(InvalidConfigOverlay, match="not a registered model") as exc_info:
        validate_config_overlay({field: "deepseek-v4-flash-vision"})

    assert "deepseek-v4-flash-vision-exp" in str(exc_info.value)
    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", ["llm_model", "memory_recall_filter_model"])
def test_validate_config_overlay_registered_model_field_passes(
    isolated_registry, unit_home, field: str
):
    validate_config_overlay({field: "claude-opus-4-6"})


def test_validate_config_overlay_none_reasoning_effort_passes(isolated_registry, unit_home):
    """None = unset (field is `str | None`); a None overlay is a legal no-op
    that pre-PR validation accepted — the range check must not regress it."""
    validate_config_overlay({"reasoning_effort": None})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # durations / timeouts — must be > 0 and finite
        ("gemini_cache_timeout_seconds", 0.0),
        ("gemini_cache_timeout_seconds", -1.0),
        ("gemini_cache_timeout_seconds", float("nan")),
        ("gemini_cache_timeout_seconds", float("inf")),
        ("heartbeat_pause_max_seconds", float("inf")),
        ("llm_stream_ttft_timeout_seconds", 0.0),
        ("llm_stream_ttft_timeout_seconds", -1.0),
        ("llm_stream_ttft_timeout_seconds", float("nan")),
        ("llm_stream_inter_chunk_timeout_seconds", -1.0),
        ("llm_stream_inter_chunk_timeout_seconds", float("inf")),
        ("memory_recall_filter_timeout_seconds", 0.0),
        ("memory_recall_filter_timeout_seconds", float("nan")),
        ("memory_recall_deadline_seconds", 0.0),
        ("memory_recall_deadline_seconds", -1.0),
        ("memory_recall_deadline_seconds", float("nan")),
        ("memory_recall_deadline_seconds", float("inf")),
        # fractions — must be in (0, 1]
        ("auto_compact_fraction", 0.0),
        ("auto_compact_fraction", 1.5),
        ("auto_compact_fraction", -0.1),
        ("auto_compact_fraction", float("nan")),
        ("auto_compact_fraction", float("inf")),
        ("compact_reminder_fraction", 0.0),
        ("compact_reminder_fraction", 1.5),
        ("compact_reminder_fraction", -0.1),
        # counts / budgets — must be >= 0
        ("auto_compact_ceiling_tokens", -1),
        ("claude_thinking_budget_tokens", -5),
        ("history_dump_keep", -1),
        ("memory_recall_filter_max_retries", -3),
        ("memory_recall_inject_k", -1),
        ("memory_recall_retrieve_k", -1),
    ],
)
def test_validate_config_overlay_out_of_range_rejected(
    isolated_registry, unit_home, field: str, value: object
):
    with pytest.raises(InvalidConfigOverlay, match=field):
        validate_config_overlay({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # exactly-on-the-bound values are legal
        ("auto_compact_fraction", 1.0),
        ("auto_compact_ceiling_tokens", 0),
        ("claude_thinking_budget_tokens", 0),
        ("history_dump_keep", 0),
        ("memory_recall_filter_max_retries", 0),
        ("llm_stream_ttft_timeout_seconds", None),  # unset sentinel
        ("llm_stream_ttft_timeout_seconds", 0.1),
        ("memory_recall_deadline_seconds", 0.1),
        ("memory_recall_deadline_seconds", 5.0),
        ("auto_compact_fraction", 0.5),
        ("compact_reminder_fraction", 0.3),
    ],
)
def test_validate_config_overlay_boundary_values_accepted(
    isolated_registry, unit_home, field: str, value: object
):
    validate_config_overlay({field: value})


def test_apply_config_overlay_mutates_plugin_config(isolated_registry, unit_home):
    """After apply, get_plugin_config returns a new instance with marker overlaid."""
    _setup_overlayable_plugin()
    assert get_plugin_config("overlay_test", _FixtureConfig).marker == ".git"
    apply_config_overlay({"marker": ".hg"})
    assert get_plugin_config("overlay_test", _FixtureConfig).marker == ".hg"


def test_apply_config_overlay_framework_scope_only_mutates_settings(
    isolated_registry, unit_home, monkeypatch: pytest.MonkeyPatch
):
    """scope='framework' applies only framework Settings half; plugin half untouched."""
    from shared.config import settings

    _setup_overlayable_plugin()
    monkeypatch.setattr(settings.lm, "llm_model", settings.lm.llm_model)  # snapshot for teardown
    assert get_plugin_config("overlay_test", _FixtureConfig).marker == ".git"

    apply_config_overlay({"llm_model": "claude-opus-4-7", "marker": ".hg"}, scope="framework")

    assert settings.lm.llm_model == "claude-opus-4-7"
    assert get_plugin_config("overlay_test", _FixtureConfig).marker == ".git"  # plugin untouched


def test_apply_config_overlay_plugin_scope_only_mutates_plugin_configs(
    isolated_registry, unit_home, monkeypatch: pytest.MonkeyPatch
):
    """scope='plugin' applies only plugin half; framework Settings untouched."""
    from shared.config import settings

    _setup_overlayable_plugin()
    original_model = settings.lm.llm_model

    apply_config_overlay({"llm_model": "claude-opus-4-7", "marker": ".hg"}, scope="plugin")

    assert settings.lm.llm_model == original_model  # framework untouched
    assert get_plugin_config("overlay_test", _FixtureConfig).marker == ".hg"


def test_llm_model_is_per_agent() -> None:
    """llm_model must be marked per_agent=True for the spawn-time overlay path."""
    from shared.config import FIELD_INFOS

    info = FIELD_INFOS["llm_model"]
    extra = info.json_schema_extra
    assert isinstance(extra, dict)
    assert extra.get("per_agent") is True  # pyright: ignore[reportUnknownMemberType]


def test_skills_to_inject_is_per_agent_overlayable(isolated_registry, unit_home) -> None:
    """A spawner overlays a per-worker skill index, so the field must be
    per_agent and resolve to the framework half (not raise like a pinned field)."""
    from shared.config import FIELD_INFOS

    info = FIELD_INFOS["skills_to_inject_into_system_prompt"]
    extra = info.json_schema_extra
    assert isinstance(extra, dict)
    assert extra.get("per_agent") is True  # pyright: ignore[reportUnknownMemberType]

    targets = resolve_overlay_targets({"skills_to_inject_into_system_prompt": ["gmail", "*"]})
    assert targets == {
        "skills_to_inject_into_system_prompt": (None, "skills_to_inject_into_system_prompt")
    }


def test_eval_isolation_fields_are_per_agent_overlayable(isolated_registry, unit_home) -> None:
    """The eval boundary is selected at spawn and its network exceptions are explicit."""
    targets = resolve_overlay_targets({"eval_isolation": True, "eval_network_allowlist": ["web"]})
    assert targets == {
        "eval_isolation": (None, "eval_isolation"),
        "eval_network_allowlist": (None, "eval_network_allowlist"),
    }
    validate_config_overlay({"eval_isolation": True, "eval_network_allowlist": ["web"]})


def test_eval_network_allowlist_rejects_unknown_capability(isolated_registry, unit_home) -> None:
    with pytest.raises(InvalidConfigOverlay, match="only accepts"):
        validate_config_overlay({"eval_network_allowlist": ["web", "shell"]})


def test_effective_config_snapshot_namespaces_plugin_fields(isolated_registry, unit_home):
    """snapshot prefixes plugin fields with `<plugin>.<field>` to avoid collisions with same-named framework fields."""
    _setup_overlayable_plugin()
    snap = effective_config_snapshot()
    assert "overlay_test.marker" in snap
    assert snap["overlay_test.marker"] == ".git"
    # framework fields are not prefixed; sensitive ones (e.g. db_url, which
    # embeds the cluster credentials) are excluded from the snapshot entirely
    assert "gateway_url" in snap
    assert "db_url" not in snap


def test_effective_config_snapshot_excludes_sensitive_fields(isolated_registry, unit_home):
    """Fields marked `sensitive=True` never enter the snapshot — it is stored as
    plain JSON on every restart_completed inbound row, so a sensitive value must
    not get a second plaintext copy there (2026-08-08 audit, P2-7)."""

    class _SensitiveConfig(BaseModel):
        model_config = ConfigDict(frozen=True)
        marker: str = Field(default=".git")
        webhook_secret: str = Field(
            default="plain-text-secret",
            json_schema_extra={"sensitive": True},
        )

    with PluginContext("sensitive_test"):
        register_plugin_config(_SensitiveConfig)
    bind_from_disk()

    snap = effective_config_snapshot()
    assert "sensitive_test.marker" in snap
    assert "sensitive_test.webhook_secret" not in snap
    assert "plain-text-secret" not in str(snap)


def test_ava_settings_plugins_attribute_access(isolated_registry, unit_home):
    """`ava._settings.plugins.<n>` returns instance; unregistered plugin name raise + lists known plugins."""
    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()

    import ava._settings as _ava_settings

    cfg = _ava_settings.plugins.test_plugin
    assert isinstance(cfg, _FixtureConfig)
    assert cfg.marker == ".git"

    with pytest.raises(AttributeError, match="Known plugins"):
        _ = _ava_settings.plugins.nonexistent_plugin


def test_syntax_fix_ruff_format_overlay_is_accepted() -> None:
    """Per-agent A/B of the ruff format gate (task #1858 follow-up, user chose
    a paired experiment): the field must accept a spawn config_overlay, like
    prompt_codeact_enabled after #719."""
    from shared.plugin_config_registry import validate_config_overlay

    validate_config_overlay({"syntax_fix_ruff_format": True})  # must not raise
    validate_config_overlay({"syntax_fix_ruff_format": False})
