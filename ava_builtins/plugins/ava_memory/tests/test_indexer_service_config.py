"""The host's indexing decision is independent of agent prompt injection."""

from pathlib import Path
from typing import cast

import pytest

from ava_builtins.plugins.ava_memory import services
from ava_builtins.plugins.ava_memory.default_config import MemoryConfig
from base.config import settings
from base.packages.plugins import enable_config, load_report
from base.packages.plugins.config_registration import (
    InvalidConfigData,
    SchemaDriftError,
    disk_image_path,
)
from base.packages.plugins.enable_config import update_all_disk_images
from ops import spec as ops_spec
from services.supervision.ava_root.inputs import InputChangedError, InputSeal
from services.supervision.ava_root_glue.manifests import build_units


@pytest.fixture
def memory_only_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = Path(services.__file__).parent
    monkeypatch.setattr(enable_config, "installed_plugin_dirs", lambda: {"ava_memory": plugin_dir})


@pytest.mark.parametrize("inject", [True, False])
def test_missing_image_runs_indexer_without_writing_home(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, inject: bool, memory_only_discovery: None
) -> None:
    monkeypatch.setattr(settings.agent, "memory_index_inject_enabled", inject)
    spec = ops_spec.plugin_services()[0]
    assert spec.session == "memory-indexer"
    assert spec.capabilities == frozenset({"gateway"})
    assert spec.gate is not None and spec.gate() is None
    assert spec.config_inputs == (unit_home / "configs" / "ava_memory" / "config.json",)
    assert not (unit_home / "configs").exists()


@pytest.mark.parametrize("inject", [True, False])
def test_independent_host_switch_disables_indexer(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, inject: bool, memory_only_discovery: None
) -> None:
    monkeypatch.setattr(settings.agent, "memory_index_inject_enabled", inject)
    image = disk_image_path("ava_memory")
    image.parent.mkdir(parents=True)
    image.write_text('{"indexer_enabled": false}\n')
    spec = ops_spec.plugin_services()[0]
    assert spec.gate is not None
    assert spec.gate() == "disabled (ava_memory.indexer_enabled off)"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not json", InvalidConfigData),
        ("[]", InvalidConfigData),
        ("{}", SchemaDriftError),
        ('{"indexer_enabled": "false"}', InvalidConfigData),
    ],
)
def test_invalid_image_fails_composition_before_a_gate_can_fail_open(
    unit_home: Path,
    content: str,
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    memory_only_discovery: None,
) -> None:
    image = disk_image_path("ava_memory")
    image.parent.mkdir(parents=True)
    image.write_text(content)
    with pytest.raises(error):
        services.services()
    failures: list[tuple[str, BaseException]] = []

    def record_failure(name: str, exc: BaseException) -> None:
        failures.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", record_failure)
    assert ops_spec.plugin_services() == ()
    assert len(failures) == 1
    assert failures[0][0] == "ava_memory"
    assert isinstance(failures[0][1], error)
    assert image.read_text() == content


def test_first_converge_materializes_and_seals_the_declared_input(
    unit_home: Path, memory_only_discovery: None
) -> None:
    before = ops_spec.plugin_services()[0]
    assert before.gate is not None and before.gate() is None
    assert not disk_image_path("ava_memory").exists()
    update = update_all_disk_images()
    memory = next(entry for entry in update.entries if entry.name == "ava_memory")
    assert memory.status == "updated"
    image = disk_image_path("ava_memory")
    assert image.read_text() == MemoryConfig().model_dump_json(indent=2) + "\n"
    spec = ops_spec.plugin_services()[0]
    assert spec.gate is not None and spec.gate() is None
    units = build_units([spec], capabilities={"gateway"}, repo_root=Path(__file__).parents[4])
    inputs = cast("list[object]", units[0]["inputs"])
    assert len(inputs) == 1
    seal = InputSeal.from_mapping(inputs[0])
    image.write_text('{"indexer_enabled": false}\n')
    with pytest.raises(InputChangedError):
        seal.require_unchanged()
    disabled = services.services()[0]
    assert disabled.gate is not None
    assert disabled.gate() == "disabled (ava_memory.indexer_enabled off)"
