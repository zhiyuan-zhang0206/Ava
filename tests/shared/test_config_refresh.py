"""refresh_data_plane_settings rebuilds the in-memory data-plane config in
place after the unit's `.env` was rewritten under the process.

The 2026-08-25 rollout incident: the local leg's `ava start` ran a
data-plane credential rotation (secret split) that rewrote `$AVA_HOME/.env`
and rotated the owner password in Postgres, while the orchestrating rollout
process kept its startup-built Settings singleton — every later data-plane
write (pin advance, compensating unpause, update-lock release) then failed
with SASL authentication. These tests pin the refresh contract: re-read the
new `.env`, rebuild only `settings.data_plane` in place, leave every other
domain untouched.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared import config, dotenv_boot

_IDENTITY_LINES = [
    f"AVA_DB_URL={os.environ['AVA_DB_URL']}",
    f"AVA_REDIS_URL={os.environ['AVA_REDIS_URL']}",
    f"AVA_CLUSTER_SECRET={os.environ['AVA_CLUSTER_SECRET']}",
    f"AVA_GATEWAY_URL={os.environ['AVA_GATEWAY_URL']}",
]


@pytest.fixture(autouse=True)
def _restore_authority_env() -> Iterator[None]:
    """`_enforce_cluster_env_authority` mutates os.environ directly (pop /
    force-assign) — snapshot the keys it touches and restore them after each
    test (same fixture as test_dotenv_boot.py)."""
    from shared.env_registry import (
        agent_runner_cluster_aliases,
        cluster_scope_aliases,
        env_identity_keys,
    )

    touched = cluster_scope_aliases() | env_identity_keys() | agent_runner_cluster_aliases()
    snapshot = {k: os.environ.get(k) for k in touched if k in os.environ}
    yield
    for key, val in snapshot.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


@pytest.fixture(autouse=True)
def _restore_data_plane(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The refresh mutates the process-global settings singleton; restore the
    pre-test data-plane sub-model after each test."""
    from shared.config import settings

    old = settings.data_plane
    yield
    settings.data_plane = old


@pytest.fixture(autouse=True)
def _restore_env_after_refresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore os.environ after every refresh test.

    `refresh_data_plane_settings` re-runs the boot env load, which writes the
    fake unit's keys (AVA_DB_ADMIN_PASSWORD etc.) into os.environ for the rest
    of the process. A later test that builds a fresh DataPlaneSettings from env
    (tests/shared/test_url_secret.py) would otherwise inherit the rotated admin
    password and fail on its re-derived URL (observed when the two files share
    a pytest worker)."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _point_env_at(monkeypatch: pytest.MonkeyPatch, env_text: str, tmp_path: Path) -> None:
    env_file = tmp_path / "unit.env"
    env_file.write_text(env_text)
    merged = tmp_path / "merged.env"
    merged.write_text(env_text + "\n".join(_IDENTITY_LINES) + "\n")
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", merged)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")


def _env_text(admin_password: str) -> str:
    return (
        "AVA_CLUSTER_SECRET=sekret\n"
        "AVA_DB_URL=postgresql://ava_main:oldpass@127.0.0.1:6433/ava_main\n"
        f"AVA_DB_ADMIN_PASSWORD={admin_password}\n"
    )


def test_refresh_picks_up_rotated_admin_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A data-plane credential rotation on disk (the 2026-08-25 secret split)
    is visible to the process after refresh: the in-memory db_url password is
    re-derived from the new AVA_DB_ADMIN_PASSWORD, and the old password is gone."""
    _point_env_at(monkeypatch, _env_text("first-pass"), tmp_path)
    config.refresh_data_plane_settings()
    from shared.config import settings

    assert "first-pass" in settings.data_plane.db_url
    assert "oldpass" not in settings.data_plane.db_url

    # Rotate on disk, refresh again — the same process now dials the new password.
    _point_env_at(monkeypatch, _env_text("rotated-pass"), tmp_path)
    config.refresh_data_plane_settings()
    assert "rotated-pass" in settings.data_plane.db_url
    assert "first-pass" not in settings.data_plane.db_url


def test_refresh_is_in_place_and_leaves_other_domains_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing `from shared.config import settings` references see the new
    sub-model (rebuild is in place on the singleton), and non-data-plane
    domains are not rebuilt (a rotation is a data-plane fact)."""
    from shared.config import settings

    old_lm = settings.lm
    _point_env_at(monkeypatch, _env_text("first-pass"), tmp_path)
    config.refresh_data_plane_settings()
    assert settings.lm is old_lm  # other domains untouched
    assert "first-pass" in settings.data_plane.db_url  # but data plane refreshed
