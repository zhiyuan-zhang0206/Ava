"""Native backend inputs belong to the root generation; storage survives it."""

from pathlib import Path

from services.ava_root.inputs import InputSeal
from shared.lgtm_local import service_environment, service_input_paths


def test_config_change_changes_generation_but_data_writes_do_not(tmp_path: Path) -> None:
    native = tmp_path / "lgtm/native"
    config = native / "config"
    config.mkdir(parents=True)
    (native / "version-loki").write_text("test")
    (native / "platform-loki").write_text("test")
    (config / "loki.yaml").write_text("limits: first")
    paths = service_input_paths(tmp_path, "loki")
    first = tuple(InputSeal.capture(path) for path in paths)
    assert service_environment("loki")["GOMEMLIMIT"] == "2GiB"
    data = native / "data"
    data.mkdir()
    (data / "chunk").write_text("new log")
    assert tuple(InputSeal.capture(path) for path in paths) == first
    (config / "loki.yaml").write_text("limits: second")
    assert tuple(InputSeal.capture(path) for path in paths) != first
