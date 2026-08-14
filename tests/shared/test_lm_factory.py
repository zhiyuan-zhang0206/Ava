"""validate_model_config's provider-key check must survive the gateway profile pop.

The gateway process no longer carries provider keys in os.environ (per-process
env assembly, Task #856) — the pop removes DEEPSEEK_API_KEY etc. from the
gateway's live env, so `settings.lm.<provider>_api_key` is None there. But the
cluster's `.env` file remains the authoritative configuration source, and the
gateway's spawn boundary must still fail fast on a genuinely missing key.
These tests pin the file fallback (regression: #1562 popped the keys and every
POST /api/agents 400'd with "requires DEEPSEEK_API_KEY which is not configured").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import settings
from shared.lm.factory import validate_model_config


@pytest.fixture
def env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the runtime_config file reader at a scratch .env and simulate the
    gateway profile (provider key absent from settings, as after the pop)."""
    import shared.runtime_config as rc

    env_path = tmp_path / ".env"
    monkeypatch.setattr(rc, "env_file_path", lambda: env_path)
    # Simulate the gateway pop: the key is absent from settings.
    monkeypatch.setattr(settings.lm, "deepseek_api_key", None)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    return env_path


def test_provider_key_present_in_settings_still_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpopped process (agent/runner): the settings value wins, no file read needed."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    assert validate_model_config(model="deepseek-v4-pro", config={}) == "deepseek-v4-pro"


def test_file_fallback_allows_key_after_gateway_pop(env_file: Path) -> None:
    """Gateway profile: key popped from settings but declared in the .env file
    → validation passes (the file is the authoritative config source)."""
    env_file.write_text("DEEPSEEK_API_KEY=sk-file-value\n")
    assert validate_model_config(model="deepseek-v4-pro", config={}) == "deepseek-v4-pro"


def test_missing_key_still_fails(env_file: Path) -> None:
    """Neither settings nor the .env file has the key → the 400 intent holds."""
    env_file.write_text("SOME_OTHER_KEY=x\n")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(model="deepseek-v4-pro", config={})


def test_unknown_model_still_fails(env_file: Path) -> None:
    env_file.write_text("DEEPSEEK_API_KEY=x\n")
    with pytest.raises(ValueError, match="unknown model"):
        validate_model_config(model="no-such-model-xyz", config={})
