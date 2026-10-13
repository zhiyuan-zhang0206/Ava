"""Owned image writes reject mixed owners and stale revisions."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base.host.env import runtime_config
from base.packages.plugin_config_admin import (
    patch_owner,
    plugin_metadata,
    write_plugin_patch,
)
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_digest,
    write_config_image,
)
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


@pytest.fixture(autouse=True)
def preserve_env_file() -> Generator[None]:
    env = runtime_config.env_file_path()
    previous = env.read_bytes() if env.exists() else None
    try:
        yield
    finally:
        if previous is None:
            env.unlink(missing_ok=True)
        else:
            env.write_bytes(previous)


def owner(path: Path) -> PluginConfigOwner:
    return PluginConfigOwner("ava_fleet", FleetConfig, path)


def test_metadata_uses_plugin_owner_and_preserves_host_cluster_policy() -> None:
    fields = {field.name: field for field in plugin_metadata() if field.owner == "ava_fleet"}
    assert set(fields) == set(FleetConfig.model_fields)
    assert fields["task_maintenance_enabled"].scope == "host"
    assert fields["task_maintenance_enabled"].remote_writable is True
    assert fields["task_maintenance_enabled"].writable is False
    assert fields["reduce_context_switch"].scope == "cluster-pinned"
    assert fields["reduce_context_switch"].per_agent is False
    assert fields["reduce_context_switch"].restart_required == "agent"
    assert all(not field.sensitive for field in fields.values())


def test_mixed_owner_patch_fails_before_any_image_write(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    with pytest.raises(InvalidConfigOverlay, match="mixes owners"):
        patch_owner({"task_escalate_n", "llm_model"})
    assert not image.exists()
    actual = patch_owner({"task_escalate_n"})
    assert actual is not None and actual.name == "ava_fleet"
    assert patch_owner({"llm_model"}) is None


def test_unknown_patch_field_is_a_request_rejection() -> None:
    with pytest.raises(InvalidConfigOverlay, match="unknown field"):
        patch_owner({"unknown_plugin_config_field"})


def test_duplicate_declaration_is_not_a_request_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.packages import plugin_config_admin as plugin_config

    first = PluginConfigOwner("first", FleetConfig, tmp_path / "first.json")
    second = PluginConfigOwner("second", FleetConfig, tmp_path / "second.json")
    monkeypatch.setattr(plugin_config, "config_owners", lambda: {"first": first, "second": second})
    with pytest.raises(ValueError, match="more than one declaration owner"):
        patch_owner({"task_escalate_n"})
    assert not first.path.exists() and not second.path.exists()


def test_image_patch_validates_whole_candidate_and_cas(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    declaration = owner(image)
    write_plugin_patch(declaration, {"task_escalate_n": 8}, set(), expected_digest=None)
    saved = image.read_bytes()
    revision = image_digest(image)
    with pytest.raises(InvalidConfigOverlay):
        write_plugin_patch(
            declaration, {"task_escalate_n": "nonsense"}, set(), expected_digest=None
        )
    assert image.read_bytes() == saved
    write_plugin_patch(
        declaration, {"reduce_context_switch": False}, set(), expected_digest=revision
    )
    saved = image.read_bytes()
    with pytest.raises(PluginConfigChangedError, match="changed"):
        write_plugin_patch(declaration, {"task_escalate_n": 9}, set(), expected_digest=revision)
    assert image.read_bytes() == saved
    with pytest.raises(PluginConfigChangedError, match="changed"):
        write_config_image(declaration, FleetConfig(), expected_digest=revision)
    values = json.loads(image.read_text())
    assert values["task_escalate_n"] == 8
    assert values["reduce_context_switch"] is False
    write_plugin_patch(declaration, {}, {"task_escalate_n"}, expected_digest=None)
    assert json.loads(image.read_text())["task_escalate_n"] == 3
