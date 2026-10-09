"""Plugin write consumers distinguish request rejection from unexpected failure."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import model_validator

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base.config.admin import plugin_config
from base.packages import plugin_config_images
from base.packages.plugin_config_images import PluginConfigOwner
from gateway.app import app
from ops.host_config import config_write_op
from ops.rpc_schemas import ConfigWriteOpResult


class ValidatedFleetConfig(FleetConfig):
    """Exercise whole-candidate validation after valid scalar coercion."""

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.task_escalate_n < 1 or not self.task_maintenance_enabled:
            raise ValueError("candidate Fleet policy rejected")
        return self


@pytest.fixture
def owned_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    image = tmp_path / "config.json"
    image.write_text(ValidatedFleetConfig().model_dump_json())
    owner = PluginConfigOwner("ava_fleet", ValidatedFleetConfig, image)
    monkeypatch.setattr(plugin_config, "config_owners", lambda: {owner.name: owner})
    return image


def write_request(
    consumer: Literal["gateway", "host"], *, bad_candidate: bool = False
) -> Response | ConfigWriteOpResult:
    if consumer == "gateway":
        with TestClient(app) as client:
            return client.put("/api/config", json={"task_escalate_n": -1 if bad_candidate else 7})
    return config_write_op({"task_maintenance_enabled": not bad_candidate}, local=False)


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_invalid_whole_candidate_is_rejected_without_writing(
    owned_image: Path, consumer: Literal["gateway", "host"]
) -> None:
    saved = owned_image.read_bytes()
    result = write_request(consumer, bad_candidate=True)
    if isinstance(result, Response):
        assert result.status_code == 400
        assert "candidate Fleet policy rejected" in result.json()["detail"]
    else:
        assert result.applied is False
        assert all(not field.ok for field in result.results.values())
        assert "candidate Fleet policy rejected" in str(result.results)
        assert result.restart_required == []
    assert owned_image.read_bytes() == saved


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_concurrent_image_change_is_rejected_without_overwriting(
    owned_image: Path, monkeypatch: pytest.MonkeyPatch, consumer: Literal["gateway", "host"]
) -> None:
    concurrent = ValidatedFleetConfig(task_escalate_n=19).model_dump_json().encode()
    original_lock = plugin_config_images.file_lock

    @contextmanager
    def lock_after_competing_write(
        path: Path, *, timeout_s: float | None = None
    ) -> Generator[None]:
        with original_lock(path, timeout_s=timeout_s):
            owned_image.write_bytes(concurrent)
            yield

    monkeypatch.setattr(plugin_config_images, "file_lock", lock_after_competing_write)
    result = write_request(consumer)
    if isinstance(result, Response):
        assert result.status_code == 409
        assert "plugin config changed" in result.json()["detail"]
    else:
        assert result.applied is False
        assert all(not field.ok for field in result.results.values())
        assert "plugin config changed" in str(result.results)
        assert result.restart_required == []
    assert owned_image.read_bytes() == concurrent


@pytest.mark.parametrize("consumer", ["gateway", "host"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_unexpected_persistence_failure_keeps_original_exception_and_bytes(
    owned_image: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: Literal["gateway", "host"],
    error_type: type[Exception],
) -> None:
    saved = owned_image.read_bytes()
    failure = error_type("unexpected persistence failure")

    def fail_write(_path: Path, _data: bytes, *, mode: int | None = None) -> None:
        raise failure

    monkeypatch.setattr(plugin_config_images, "write_bytes_atomic", fail_write)
    with pytest.raises(error_type) as captured:
        write_request(consumer)
    assert captured.value is failure
    assert owned_image.read_bytes() == saved


@pytest.mark.parametrize("consumer", ["gateway", "host"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_unexpected_owner_failure_keeps_original_exception_and_bytes(
    owned_image: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: Literal["gateway", "host"],
    error_type: type[Exception],
) -> None:
    from gateway.routers import config as config_router
    from ops import host_config

    saved = owned_image.read_bytes()
    failure = error_type("unexpected declaration dependency failure")

    def fail_owner(_fields: set[str]) -> PluginConfigOwner | None:
        raise failure

    module = config_router if consumer == "gateway" else host_config
    monkeypatch.setattr(module, "patch_owner", fail_owner)
    with pytest.raises(error_type) as captured:
        write_request(consumer)
    assert captured.value is failure
    assert owned_image.read_bytes() == saved
