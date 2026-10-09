"""Provider-loader failures preserve Python errors and permit a fresh corrected build."""

import sys
from collections.abc import Callable
from types import ModuleType, TracebackType
from typing import NoReturn

import pytest

from base import paths
from base.lm.catalog import CatalogBuilder
from base.lm.plugin_providers import build_model_catalog
from base.lm.provider_api import ProviderContribution
from base.lm.tests.providers.provider_plugin_support import provider_plugin as provider_plugin


@pytest.mark.parametrize("phase", ["import", "contribute", "install", "build"])
@pytest.mark.parametrize(
    "error", [TypeError("bad provider code"), ValueError("invalid provider input")]
)
def test_unknown_provider_error_preserves_identity_and_traceback_then_recovers(
    provider_plugin: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: Exception,
) -> None:
    """No import, declaration or final-build failure can publish a partial catalog."""
    provider_plugin(prefix="kept-", model="kept-1", dir_name="a_kept_provider")
    provider_plugin(dir_name="z_broken_provider")
    provider_py = paths.plugins_dir() / "z_broken_provider" / "provider.py"
    valid_source = provider_py.read_text()
    original_traceback: list[TracebackType] = []

    def raise_error() -> NoReturn:
        raise error

    def fail() -> NoReturn:
        try:
            raise_error()
        except Exception as raised:
            assert raised.__traceback__ is not None
            original_traceback.append(raised.__traceback__)
            raise

    def fail_build(_builder: CatalogBuilder) -> NoReturn:
        fail()

    original_install = CatalogBuilder.install

    def fail_install(
        builder: CatalogBuilder, plugin: str, contribution: ProviderContribution
    ) -> None:
        if plugin == "z_broken_provider":
            assert builder.has_bindings
            fail()
        original_install(builder, plugin, contribution)

    marker = ModuleType("provider_loader_failure_marker")
    marker.__dict__["fail"] = fail
    with monkeypatch.context() as failing:
        failing.setitem(sys.modules, marker.__name__, marker)
        if phase == "import":
            provider_py.write_text("from provider_loader_failure_marker import fail\nfail()\n")
        elif phase == "contribute":
            provider_py.write_text(
                "from provider_loader_failure_marker import fail\ndef contribute():\n    fail()\n"
            )
        elif phase == "install":
            failing.setattr(CatalogBuilder, "install", fail_install)
        else:
            failing.setattr(CatalogBuilder, "build", fail_build)

        with pytest.raises(type(error)) as raised:
            build_model_catalog()

        assert raised.value is error
        frames: list[TracebackType] = []
        traceback = raised.value.__traceback__
        while traceback is not None:
            frames.append(traceback)
            traceback = traceback.tb_next
        assert original_traceback[0] in frames
        if phase != "build":
            assert "plugins.z_broken_provider.provider" not in sys.modules

    provider_py.write_text(valid_source)
    corrected = build_model_catalog()
    assert {"kept-1", "testp-1"} <= corrected.models.keys()
