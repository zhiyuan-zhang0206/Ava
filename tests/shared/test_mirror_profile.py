"""The `--mirror NAME` profiles and how they load.

`install.sh --mirror NAME` sources `scripts/mirrors/NAME.env` and copies it to
`~/.ava/mirror.env`, which `shared.dotenv_boot.load_ava_env` loads after `.env`.
These tests pin two things the design leans on: the `cn` profile carries the
package-manager index/registry vars, and a mirror var loaded from the file never
overrides one already in the real environment (precedence: env > .env > mirror.env).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values, load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CN_PROFILE = _REPO_ROOT / "scripts" / "mirrors" / "cn.env"


def test_cn_profile_carries_the_package_manager_vars() -> None:
    """The cn profile sets exactly the index/registry env each package manager
    honors — PyPI (uv), npm, and Homebrew bottles + API."""
    values = dotenv_values(_CN_PROFILE)
    assert values["UV_DEFAULT_INDEX"], "PyPI index for uv sync"
    assert values["npm_config_registry"], "npm registry for npm ci"
    assert values["HOMEBREW_BOTTLE_DOMAIN"], "Homebrew bottle mirror (macOS)"
    assert values["HOMEBREW_API_DOMAIN"], "Homebrew formulae API mirror (macOS)"
    # Every value is a concrete https endpoint, not a placeholder.
    for key, val in values.items():
        assert val and val.startswith("https://"), f"{key} should be an https URL, got {val!r}"


def test_mirror_env_does_not_override_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror var already exported (or set from .env) wins over mirror.env —
    load_dotenv never overrides an already-set key, which is what gives the
    documented `env > .env > mirror.env` precedence."""
    mirror_env = tmp_path / "mirror.env"
    mirror_env.write_text("UV_DEFAULT_INDEX=https://mirror.example/simple\n")

    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://explicit.example/simple")
    load_dotenv(mirror_env)  # must NOT clobber the explicit value
    assert os.environ["UV_DEFAULT_INDEX"] == "https://explicit.example/simple"


def test_mirror_env_fills_unset_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a mirror var is not otherwise set, mirror.env supplies it."""
    mirror_env = tmp_path / "mirror.env"
    mirror_env.write_text("npm_config_registry=https://registry.example\n")

    # npm reads the lowercase `npm_config_<key>` env form; the uppercase spelling
    # SIM112 prefers is not what npm honors, so the lowercase key is load-bearing.
    monkeypatch.delenv("npm_config_registry", raising=False)
    load_dotenv(mirror_env)
    assert os.environ["npm_config_registry"] == "https://registry.example"  # noqa: SIM112
