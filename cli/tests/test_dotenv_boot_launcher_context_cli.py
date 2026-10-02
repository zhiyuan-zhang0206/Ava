"""The CLI entry records the launcher profile and the authority pass, reading the recorded context, keeps the undeclared launcher projections (#4334)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from base.host.env import dotenv_boot
from tests.fixtures.units import use_env_files

_NO_DATA_PLANE_IDENTITY_LINES = (
    f"AVA_CLUSTER_SECRET={os.environ['AVA_CLUSTER_SECRET']}",
    f"AVA_GATEWAY_URL={os.environ['AVA_GATEWAY_URL']}",
)


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """`_enforce_cluster_env_authority` and the CLI entry mutate os.environ directly,
    not through monkeypatch: put the whole environment back after each test."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _point_env_at_without_data_plane_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An env file that declares neither data-plane URL — the pure agent-runner
    shape for AVA_DB_URL and AVA_REDIS_URL together (#4334)."""
    env_file = tmp_path / "no-data-plane.env"
    env_file.write_text(
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n" + "\n".join(_NO_DATA_PLANE_IDENTITY_LINES) + "\n"
    )
    use_env_files(monkeypatch, env_file)


def test_cli_entry_records_launcher_profile_and_keeps_undeclared_projections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full CLI shape (#4334): the entry helper pops the live marker and
    records it, and the later authority pass — reading the recorded context —
    keeps both undeclared launcher projections. This is the health-probe flow
    that was red: agent child -> CLI pop -> drop -> sentinel (env-class)."""
    from cli.main import _normalize_process_profile

    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    # The entry helper records the launcher profile next. Register the absent key (setitem)
    # before removing it: `delitem(raising=False)` on an absent key records nothing, so the
    # recorded value would outlive the test.
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "")
    monkeypatch.delitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY)
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    _point_env_at_without_data_plane_urls(monkeypatch, tmp_path)
    monkeypatch.setitem(
        os.environ,
        "AVA_DB_URL",
        "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava",
    )
    monkeypatch.setitem(
        os.environ, "AVA_REDIS_URL", "redis://ava:runtime-password@127.0.0.1:6380/0"
    )
    # Companion: inherited keys the `.env` does not declare still drop in the
    # same pass — the exemption must not spill.
    monkeypatch.setitem(os.environ, "AVA_APP_PORT", "3001")

    _normalize_process_profile()

    assert "AVA_PROCESS_PROFILE" not in os.environ
    assert os.environ[dotenv_boot.LAUNCHER_PROFILE_ENV_KEY] == "agent"

    dotenv_boot._enforce_cluster_env_authority(dotenv_boot.resolve_ava_home())

    assert os.environ["AVA_DB_URL"] == "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"
    assert os.environ["AVA_REDIS_URL"] == "redis://ava:runtime-password@127.0.0.1:6380/0"
    assert "AVA_APP_PORT" not in os.environ
