"""Tests for Plugin config registration + disk image (`agent/config.py`).

Covers:
- bind_plugin_config: non-BaseModel raise, duplicate bind raise, auto-write default (disk missing),
  instantiation OK, schema drift raise, undo drops the binding
- install: a plugin whose config does not bind is refused whole and reported while others install
- merge_disk_image_schema: new fields fill default, removed fields dropped, unchanged no-op
- is_per_agent_field: json_schema_extra={"per_agent": True} recognition
- ava.sdk_surface.settings.plugins.<n> attribute access wrong name raise + list known plugins
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.host.env.agent_slices import AgentSlices
from base.packages.plugins import load_report
from base.packages.plugins.config_registration import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    DuplicateRegistration,
    InvalidConfigOverlay,
    SchemaDriftError,
    apply_config_overlay,
    bind_plugin_config,
    disk_image_path,
    effective_config_snapshot,
    get_plugin_config,
    is_per_agent_field,
    merge_disk_image_schema,
    read_config_image,
    resolve_overlay_targets,
    validate_config_overlay,
    write_default_disk_image,
)


class _FixtureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    flag: bool = Field(default=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})


def test_read_config_image_defaults_without_creating_home(tmp_path: Path) -> None:
    image = tmp_path / "absent-home" / "configs" / "fixture" / "config.json"
    config = read_config_image(_FixtureConfig, image)
    assert config.flag is True
    assert config.marker == ".git"
    assert not image.parent.parent.parent.exists()


def test_read_config_image_returns_disk_values_without_registration(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    content = '{"flag": false, "marker": "custom"}\n'
    image.write_text(content)
    before = (dict(_PLUGIN_CONFIG_CLASSES), dict(_PLUGIN_CONFIGS))
    config = read_config_image(_FixtureConfig, image)
    assert (config.flag, config.marker) == (False, "custom")
    assert image.read_text() == content
    assert before == (_PLUGIN_CONFIG_CLASSES, _PLUGIN_CONFIGS)


@pytest.mark.parametrize(
    "content", ['{"flag": false}', '{"flag": false, "marker": ".git", "extra": 1}']
)
def test_read_config_image_rejects_schema_drift_without_repair(
    tmp_path: Path, content: str
) -> None:
    image = tmp_path / "config.json"
    image.write_text(content)
    with pytest.raises(SchemaDriftError, match="schema drift"):
        read_config_image(_FixtureConfig, image)
    assert image.read_text() == content


@pytest.fixture
def isolated_registry():
    """Per-test clean registry — avoids cross-test pollution.

    This fixture teardown re-registers to restore initial state
    (note: registration order doesn't matter; zero cross-test impact).
    """
    # Snapshot before
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    yield
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


def test_bind_non_basemodel_raises(isolated_registry):
    class _NotBaseModel:
        pass

    with pytest.raises(TypeError, match="BaseModel subclass"):
        bind_plugin_config("test_plugin", _NotBaseModel)  # type: ignore[arg-type]


def test_bind_duplicate_raises(isolated_registry, unit_home):
    bind_plugin_config("test_plugin", _FixtureConfig)
    with pytest.raises(DuplicateRegistration, match="test_plugin"):
        bind_plugin_config("test_plugin", _FixtureConfig)


def test_bind_auto_writes_default_when_missing(isolated_registry, unit_home):
    """disk image missing → bind_plugin_config auto-writes default + instantiation OK."""
    bind_plugin_config("test_plugin", _FixtureConfig)

    cfg = get_plugin_config("test_plugin", AgentSlices.resolve(), _FixtureConfig)
    assert cfg.flag is True
    assert cfg.marker == ".git"
    # Disk image should have been written
    img = disk_image_path("test_plugin")
    assert img.exists()
    assert json.loads(img.read_text()) == {"flag": True, "marker": ".git"}


def test_bind_reads_existing_image(isolated_registry, unit_home):
    """disk image exists and schema matches → bind uses disk values, not cls defaults."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": False, "marker": ".hg"}))  # pyright: ignore[reportUnknownMemberType]

    bind_plugin_config("test_plugin", _FixtureConfig)

    cfg = get_plugin_config("test_plugin", AgentSlices.resolve(), _FixtureConfig)
    assert cfg.flag is False
    assert cfg.marker == ".hg"


def test_bind_schema_drift_raises(isolated_registry, unit_home):
    """disk image field set doesn't match cls → SchemaDriftError to guide update."""
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": True, "marker": ".git", "extra_field": 42}))  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(SchemaDriftError, match="schema drift"):
        bind_plugin_config("test_plugin", _FixtureConfig)
    assert "test_plugin" not in _PLUGIN_CONFIGS


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

    Dropping is key: the field-set strict equality check in bind_plugin_config requires disk == cls; keeping leftover fields
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
    plugin-config-images step), bind_plugin_config does not raise SchemaDriftError.

    This is the real production scenario where spawn resulted in terminated on startup (compact_tail_messages
    was removed from schema, leftover disk image). Before the fix, merge kept the leftover fields, bind still crashed.
    """
    tmp_path = unit_home
    img = tmp_path / "configs" / "test_plugin" / "config.json"
    img.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    img.write_text(json.dumps({"flag": True, "marker": ".git", "obsolete": "old"}))  # pyright: ignore[reportUnknownMemberType]

    merge_disk_image_schema("test_plugin", _FixtureConfig)

    bind_plugin_config(
        "test_plugin", _FixtureConfig
    )  # before fix, would raise SchemaDriftError here

    cfg = get_plugin_config("test_plugin", AgentSlices.resolve(), _FixtureConfig)
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


def test_write_default_disk_image_preserves_existing(
    isolated_registry: None, unit_home: Path
) -> None:
    """Initialization rejects an existing authority instead of resetting explicit values."""
    image = unit_home / "configs" / "test_plugin" / "config.json"
    image.parent.mkdir(parents=True)
    content = json.dumps({"flag": False, "marker": ".old"})
    image.write_text(content)
    with pytest.raises(RuntimeError, match="changed before owned image write"):
        write_default_disk_image("test_plugin", _FixtureConfig)
    assert image.read_text() == content


def test_default_initialization_rejects_file_created_after_missing_check(
    isolated_registry: None, unit_home: Path
) -> None:
    image = unit_home / "configs" / "test_plugin" / "config.json"
    assert not image.exists()
    image.parent.mkdir(parents=True)
    image.write_text(_FixtureConfig(flag=False, marker="concurrent").model_dump_json())
    with pytest.raises(RuntimeError, match="changed before owned image write"):
        write_default_disk_image("test_plugin", _FixtureConfig)
    assert read_config_image(_FixtureConfig, image).marker == "concurrent"


def test_is_per_agent_field_metadata(isolated_registry, unit_home):
    """json_schema_extra={"per_agent": True} → is_per_agent_field True; otherwise False."""
    bind_plugin_config("test_plugin", _FixtureConfig)

    assert is_per_agent_field("test_plugin", "marker") is True  # has per_agent metadata
    assert is_per_agent_field("test_plugin", "flag") is False  # no per_agent metadata
    assert is_per_agent_field("test_plugin", "nonexistent") is False
    assert is_per_agent_field("unknown_plugin", "x") is False


# ── overlay (PR-E) ─────────────────────────────────────────────────────────


def _setup_overlayable_plugin():
    """Register a frozen Config with per_agent=True fields, run bind_plugin_config.

    Requires the `unit_home` fixture active in the calling test (AVA_HOME
    pointing at a per-test tmp dir) so bind_plugin_config writes the disk image there,
    not into the shared session home — callers must declare `unit_home`.
    """
    bind_plugin_config("overlay_test", _FixtureConfig)


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


def test_validate_config_overlay_uses_declaring_model_in_gateway_profile(
    monkeypatch: pytest.MonkeyPatch, isolated_registry, unit_home
) -> None:
    """Framework validation must not read a domain absent from the gateway profile."""
    import base.config as base_config
    from base.config import Settings

    monkeypatch.setattr(base_config, "settings", Settings(profile="gateway"))

    validate_config_overlay({"completion_notice_policy": "hourly"})
    with pytest.raises(InvalidConfigOverlay, match="completion_notice_policy"):
        validate_config_overlay({"completion_notice_policy": "bogus"})


def test_validate_config_overlay_runs_declaring_model_validators_in_gateway_profile(
    monkeypatch: pytest.MonkeyPatch, isolated_registry, unit_home
) -> None:
    """Declaring-model validation preserves before and field validators."""
    import base.config as base_config
    from base.config import Settings

    monkeypatch.setattr(base_config, "settings", Settings(profile="gateway"))

    validate_config_overlay({"skills_to_expand_at_start": "a,b"})
    with pytest.raises(InvalidConfigOverlay, match="only accepts"):
        validate_config_overlay({"eval_network_allowlist": ["web", "shell"]})


def test_validate_config_overlay_unknown_llm_model_raises(isolated_registry, unit_home):
    with pytest.raises(InvalidConfigOverlay, match="not a registered model") as exc_info:
        validate_config_overlay({"llm_model": "deepseek-v4-flash-vision"})

    assert "deepseek-flash" in str(exc_info.value)


def test_validate_config_overlay_registered_llm_model_passes(isolated_registry, unit_home):
    validate_config_overlay({"llm_model": "claude-opus-5"})


def test_validate_overlay_is_self_sufficient_in_a_fresh_process() -> None:
    """File-level isolation (task #3138, same class as the compact gate's budget):
    a process whose FIRST registry use is this validation must pass a registered
    model id — pre-fix the empty registry false-rejected it ("valid models: "
    empty). This test process already has the registry loaded (the other overlay
    tests here depend on it), so the scenario runs in a fresh interpreter."""
    code = textwrap.dedent(
        """
        from base.lm import plugin_providers
        from base.packages.plugins.config_registration import validate_config_overlay

        assert plugin_providers._STATE.catalog is None, "fresh process must start with no catalog"
        validate_config_overlay({"llm_model": "deepseek-flash"})
        print("ok")
        """
    )
    result = subprocess.run(  # noqa: S603 — our own venv python + a literal script
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


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

    assert "deepseek-flash" in str(exc_info.value)
    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", ["llm_model", "memory_recall_filter_model"])
def test_validate_config_overlay_registered_model_field_passes(
    isolated_registry, unit_home, field: str
):
    validate_config_overlay({field: "claude-opus-5"})


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
        ("llm_stream_total_timeout_seconds", 0.0),
        ("llm_stream_total_timeout_seconds", float("inf")),
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
        ("llm_stream_total_timeout_seconds", None),
        ("llm_stream_total_timeout_seconds", 3600.0),
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
    assert get_plugin_config("overlay_test", AgentSlices.resolve(), _FixtureConfig).marker == ".git"
    apply_config_overlay({"marker": ".hg"})
    assert get_plugin_config("overlay_test", AgentSlices.resolve(), _FixtureConfig).marker == ".hg"


def test_apply_config_overlay_framework_scope_only_mutates_settings(
    isolated_registry, unit_home, monkeypatch: pytest.MonkeyPatch
):
    """scope='framework' applies only framework Settings half; plugin half untouched."""
    from base.config import settings

    _setup_overlayable_plugin()
    monkeypatch.setattr(settings.lm, "llm_model", settings.lm.llm_model)  # snapshot for teardown
    assert get_plugin_config("overlay_test", AgentSlices.resolve(), _FixtureConfig).marker == ".git"

    apply_config_overlay({"llm_model": "claude-opus-5", "marker": ".hg"}, scope="framework")

    assert settings.lm.llm_model == "claude-opus-5"
    assert (
        get_plugin_config("overlay_test", AgentSlices.resolve(), _FixtureConfig).marker == ".git"
    )  # plugin untouched


def test_apply_config_overlay_plugin_scope_only_mutates_plugin_configs(
    isolated_registry, unit_home, monkeypatch: pytest.MonkeyPatch
):
    """scope='plugin' applies only plugin half; framework Settings untouched."""
    from base.config import settings

    _setup_overlayable_plugin()
    original_model = settings.lm.llm_model

    apply_config_overlay({"llm_model": "claude-opus-5", "marker": ".hg"}, scope="plugin")

    assert settings.lm.llm_model == original_model  # framework untouched
    assert get_plugin_config("overlay_test", AgentSlices.resolve(), _FixtureConfig).marker == ".hg"


def test_skills_to_inject_is_per_agent_overlayable(isolated_registry, unit_home) -> None:
    """A spawner overlays a per-worker skill index, so the field must be
    per_agent and resolve to the framework half (not raise like a pinned field)."""
    from base.config import FIELD_INFOS

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

    bind_plugin_config("sensitive_test", _SensitiveConfig)

    snap = effective_config_snapshot()
    assert "sensitive_test.marker" in snap
    assert "sensitive_test.webhook_secret" not in snap
    assert "plain-text-secret" not in str(snap)


def test_syntax_fix_ruff_format_overlay_is_accepted() -> None:
    """Per-agent A/B of the ruff format gate (task #1858 follow-up, user chose
    a paired experiment): the field must accept a spawn config_overlay, like
    prompt_codeact_enabled after #719."""
    from base.packages.plugins.config_registration import validate_config_overlay

    validate_config_overlay({"syntax_fix_ruff_format": True})  # must not raise
    validate_config_overlay({"syntax_fix_ruff_format": False})


def test_bind_undo_drops_the_binding(isolated_registry, unit_home):
    undo = bind_plugin_config("test_plugin", _FixtureConfig)
    assert is_per_agent_field("test_plugin", "marker") is True
    undo()

    assert "test_plugin" not in _PLUGIN_CONFIGS
    assert is_per_agent_field("test_plugin", "marker") is False
    bind_plugin_config("test_plugin", _FixtureConfig)  # a rebind after the undo is legal


def test_install_refuses_a_plugin_whose_config_does_not_bind_and_installs_the_rest(
    isolated_registry, unit_home, monkeypatch: pytest.MonkeyPatch
):
    """A config that cannot bind (SchemaDriftError) is a load failure of that plugin alone: it is
    rolled back whole (its earlier namespace too), reported, and absent from the returned registry,
    while the plugins around it install."""
    from types import SimpleNamespace

    import ava
    from ava.sdk_surface import install
    from base.packages.plugins.extensions import (
        ExtensionRegistry,
        PluginContributions,
        SdkNamespace,
    )

    drifted = disk_image_path("drifted")
    drifted.parent.mkdir(parents=True)
    drifted.write_text(json.dumps({"flag": True, "marker": ".git", "extra_field": 42}))

    reported: list[tuple[str, BaseException]] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", _capture)
    registry = ExtensionRegistry(
        (
            (
                "drifted",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("drifted_ns", SimpleNamespace()),),
                    config=_FixtureConfig,
                ),
            ),
            (
                "healthy",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("healthy_ns", SimpleNamespace()),),
                    config=_FixtureConfig,
                ),
            ),
        )
    )

    admitted = install.install(registry)
    try:
        assert [name for name, _ in admitted.plugins] == ["healthy"]
        assert [name for name, _ in reported if name == "drifted"] == ["drifted"]
        assert isinstance(
            next(exc for name, exc in reported if name == "drifted"), SchemaDriftError
        )
        assert not hasattr(ava, "drifted_ns")
        assert hasattr(ava, "healthy_ns")
        assert "drifted" not in _PLUGIN_CONFIGS
        assert "healthy" in _PLUGIN_CONFIGS
    finally:
        install.uninstall()
    assert not hasattr(ava, "healthy_ns")
    assert "healthy" not in _PLUGIN_CONFIGS
