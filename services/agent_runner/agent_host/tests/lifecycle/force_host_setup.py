"""Provider-independent model validation for fake hosted force tests."""

import pytest

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog


def _allow_model_config(
    *,
    model: str | None = None,
    overrides: ModelOverrides | None = None,
    catalog: ModelCatalog,
    llm_override: str,
) -> str:
    """Return the model name unchanged; fake-host tests carry no provider keys."""
    del overrides, catalog, llm_override
    return model or "deepseek-v4-flash-vision-exp"


@pytest.fixture(autouse=True)
def host_wakes_need_no_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fake-host wakes independent of installed provider credentials."""
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", _allow_model_config
    )


__all__ = ["host_wakes_need_no_provider_credentials"]
