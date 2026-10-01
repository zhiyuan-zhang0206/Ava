"""The provider-key mock the ava, gateway and integration tests share.

One definition, registered once per path-scoped module that imports it (importing
a fixture into a module registers it under that name there), so each of those
modules keeps its autouse batch in one alphabetical order.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all API keys to dummy values so spawn validation passes."""
    from base.config import settings as _settings

    for attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "dashscope_api_key",
    ):
        monkeypatch.setattr(_settings.lm, attr, SecretStr("sk-test"))
