"""Owned image writes reject mixed owners and preserve explicit legacy inputs."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base.config.admin.plugin_config import (
    PluginConfigOwner,
    image_digest,
    import_legacy_config,
    patch_owner,
    plugin_metadata,
    write_config_image,
    write_plugin_patch,
)
from base.host.env import runtime_config
from base.packages.plugins.config_registration import InvalidConfigData, read_authority_config


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
    with pytest.raises(ValueError, match="mixes owners"):
        patch_owner({"task_escalate_n", "llm_model"})
    assert not image.exists()
    actual = patch_owner({"task_escalate_n"})
    assert actual is not None and actual.name == "ava_fleet"
    assert patch_owner({"llm_model"}) is None


def test_image_patch_validates_whole_candidate_and_cas(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    declaration = owner(image)
    write_plugin_patch(declaration, {"task_escalate_n": 8}, set(), expected_digest=None)
    saved = image.read_bytes()
    revision = image_digest(image)
    with pytest.raises(ValueError):
        write_plugin_patch(
            declaration, {"task_escalate_n": "nonsense"}, set(), expected_digest=None
        )
    assert image.read_bytes() == saved
    write_plugin_patch(
        declaration, {"reduce_context_switch": False}, set(), expected_digest=revision
    )
    with pytest.raises(RuntimeError, match="changed"):
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
