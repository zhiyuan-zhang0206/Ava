from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.tests.configuration import snapshot_process_config
from base.lm.catalog import ModelCatalog
from base.packages.plugins.enable_config import discover_plugins, set_local_enabled
from base.packages.tests.lm.providers.provider_plugin_support import (
    provider_plugin as provider_plugin,
)
from base.paths import plugins_dir
from gateway.http.model_catalog import (
    CatalogLoadFailure,
    ModelCatalogUnavailableError,
    load_model_catalog,
    require_catalog,
)
from tests.fixtures.gateway_config import gateway_test_client


def test_catalog_failure_preserves_identity_and_traceback() -> None:
    failure = RuntimeError("no provider plugins enabled")

    def fail_provider_load() -> ModelCatalog:
        raise failure

    result = load_model_catalog(fail_provider_load)
    assert isinstance(result, CatalogLoadFailure)
    assert result.error is failure
    traceback = failure.__traceback__
    assert traceback is not None
    with pytest.raises(ModelCatalogUnavailableError) as first:
        require_catalog(result)
    with pytest.raises(ModelCatalogUnavailableError) as second:
        require_catalog(result)
    assert first.value is not second.value
    assert first.value.__cause__ is second.value.__cause__ is failure
    assert failure.__traceback__ is traceback


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(1)])
def test_process_exit_does_not_become_model_unavailable(failure: BaseException) -> None:
    def exit_provider_load() -> ModelCatalog:
        raise failure

    with pytest.raises(type(failure)) as caught:
        load_model_catalog(exit_provider_load)
    assert caught.value is failure


def test_zero_provider_keeps_management_available(
    provider_plugin: Callable[..., None], *, machine_identity: MachineIdentity
) -> None:
    del provider_plugin
    for name in discover_plugins():
        set_local_enabled(name, enabled=False)
    with gateway_test_client(machine=machine_identity) as client:
        assert client.get("/api/settings").status_code == 200
        response = client.get("/api/models")
        assert response.status_code == 503
        assert response.json()["code"] == "model_catalog_unavailable"


@pytest.fixture
def broken_provider(provider_plugin: Callable[..., None]) -> tuple[Path, str]:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="a_kept_provider")
    provider_plugin(dir_name="z_broken_provider")
    provider_path = plugins_dir() / "z_broken_provider" / "provider.py"
    valid_provider = provider_path.read_text()
    provider_path.write_text('raise RuntimeError("broken provider declaration")\n')
    return provider_path, valid_provider


@pytest.mark.usefixtures("served_gateway_home", "broken_provider")
def test_broken_provider_keeps_auth_and_management_available(
    *, machine_identity: MachineIdentity
) -> None:
    boot = snapshot_process_config()
    credential = settings.data_plane.cluster_secret
    boot.set_field("cluster_secret", credential)
    with gateway_test_client(machine=machine_identity, boot=boot) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/models", headers={"Authorization": ""}).status_code == 401
        login = client.post("/api/auth/login", json={"password": credential})
        assert login.status_code == 200, login.text
        assert client.get("/api/auth/check").status_code == 200
        assert client.get("/api/settings").status_code == 200
        write = client.put("/api/config", json={"timezone": "Asia/Shanghai"})
        assert write.status_code == 200, write.text
        assert write.json()["applied"] is True
        config = client.get("/api/config")
        assert config.status_code == 200, config.text


@pytest.mark.usefixtures("served_gateway_home")
def test_provider_repair_restores_catalog_after_safe_failure(
    broken_provider: tuple[Path, str], *, machine_identity: MachineIdentity
) -> None:
    provider_path, valid_provider = broken_provider
    boot = snapshot_process_config()
    with gateway_test_client(machine=machine_identity, boot=boot) as client:
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 503, bootstrap.text
        assert "MEIVA_DB_URL" not in bootstrap.json()
        response = client.get("/api/models")
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "model_catalog_unavailable"
        assert response.json()["retryable"] is False
        assert "broken provider declaration" not in response.text
    provider_path.write_text(valid_provider)
    with gateway_test_client(machine=machine_identity, boot=boot) as client:
        assert client.get("/api/models").status_code == 200
