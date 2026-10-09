"""Owned image writes reject mixed owners and preserve explicit legacy inputs."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base.config.admin.plugin_config import (
    import_legacy_config,
    patch_owner,
    plugin_metadata,
    write_plugin_patch,
)
from base.host.env import runtime_config
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_digest,
    write_config_image,
)
from base.packages.plugins.config_registration import (
    InvalidConfigData,
    InvalidConfigOverlay,
    read_authority_config,
)


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
    from base.config.admin import plugin_config

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


def test_pending_legacy_read_is_pure_and_once_import_preserves_values(tmp_path: Path) -> None:
    image = tmp_path / "configs" / "ava_fleet" / "config.json"
    env = runtime_config.env_file_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("AVA_TASK_ESCALATE_N=9\nAVA_REDUCE_CONTEXT_SWITCH=false\nOTHER=value\n")
    with pytest.raises(InvalidConfigData, match="plugins update"):
        read_authority_config("ava_fleet", FleetConfig, image)
    assert not image.parent.exists()
    assert import_legacy_config(owner(image)) is True
    adopted = read_authority_config("ava_fleet", FleetConfig, image)
    assert adopted.task_escalate_n == 9
    assert adopted.reduce_context_switch is False
    assert env.read_text() == "OTHER=value\n"
    assert import_legacy_config(owner(image)) is False


def test_import_conflict_never_discards_explicit_input(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    image.write_text(FleetConfig(task_escalate_n=7).model_dump_json())
    env = runtime_config.env_file_path()
    env.write_text("AVA_TASK_ESCALATE_N=9\n")
    saved = image.read_text()
    with pytest.raises(ValueError, match="conflicts"):
        import_legacy_config(owner(image))
    assert image.read_text() == saved
    assert env.read_text() == "AVA_TASK_ESCALATE_N=9\n"


def test_import_removal_failure_leaves_same_value_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.host.env import dotenv_file

    image = tmp_path / "config.json"
    env = runtime_config.env_file_path()
    env.write_text("AVA_TASK_ESCALATE_N=9\n")

    def fail_removal(
        _path: Path, _keys: set[str], *, expected_values: dict[str, str] | None = None
    ) -> dict[str, str]:
        assert expected_values is not None
        raise OSError("disk")

    with monkeypatch.context() as interception:
        interception.setattr(dotenv_file, "remove_env", fail_removal)
        with pytest.raises(OSError, match="disk"):
            import_legacy_config(owner(image))
    assert json.loads(image.read_text())["task_escalate_n"] == 9
    assert env.read_text() == "AVA_TASK_ESCALATE_N=9\n"
    assert import_legacy_config(owner(image)) is True
    assert "AVA_TASK_ESCALATE_N" not in env.read_text()


def test_legacy_import_reconciles_old_schema_before_adopting_nondefault_values(
    tmp_path: Path,
) -> None:
    image = tmp_path / "config.json"
    image.write_text('{"agent_standing_directives": []}\n')
    env = runtime_config.env_file_path()
    env.write_text("AVA_TASK_MAINTENANCE_ENABLED=false\nAVA_TASK_ESCALATE_N=9\nOTHER=preserved\n")

    assert import_legacy_config(owner(image)) is True
    adopted = read_authority_config("ava_fleet", FleetConfig, image)
    assert adopted == FleetConfig(task_maintenance_enabled=False, task_escalate_n=9)
    assert env.read_text() == "OTHER=preserved\n"
    saved = image.read_bytes()
    assert import_legacy_config(owner(image)) is False
    assert image.read_bytes() == saved


@pytest.mark.parametrize("stored", [7, "invalid"])
def test_drifted_image_rejects_explicit_conflict_or_invalid_value_before_writing(
    tmp_path: Path, stored: int | str
) -> None:
    image = tmp_path / "config.json"
    image.write_text(json.dumps({"task_escalate_n": stored, "retired": "preserve on failure"}))
    env = runtime_config.env_file_path()
    env.write_text("AVA_TASK_ESCALATE_N=9\nOTHER=preserved\n")
    image_before, env_before = image.read_bytes(), env.read_bytes()

    error = ValueError if isinstance(stored, int) else InvalidConfigData
    with pytest.raises(error):
        import_legacy_config(owner(image))
    assert image.read_bytes() == image_before
    assert env.read_bytes() == env_before


def test_drifted_image_import_removal_failure_keeps_nondefault_same_value_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.host.env import dotenv_file

    image = tmp_path / "config.json"
    image.write_text('{"agent_standing_directives": []}\n')
    env = runtime_config.env_file_path()
    env.write_text("AVA_TASK_MAINTENANCE_ENABLED=false\nOTHER=preserved\n")
    before = env.read_bytes()

    def fail_removal(*_args: object, **_kwargs: object) -> None:
        raise OSError("remove failed")

    with monkeypatch.context() as interception:
        interception.setattr(dotenv_file, "remove_env", fail_removal)
        with pytest.raises(OSError, match="remove failed"):
            import_legacy_config(owner(image))
    assert json.loads(image.read_text())["task_maintenance_enabled"] is False
    assert env.read_bytes() == before
    assert import_legacy_config(owner(image)) is True
    assert env.read_text() == "OTHER=preserved\n"
    assert import_legacy_config(owner(image)) is False
