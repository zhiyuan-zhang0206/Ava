"""The CLI entry records the launcher profile and the authority pass, reading the recorded context, keeps the undeclared launcher projections (#4334)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from base.host.env import dotenv_boot
from base.host.env.tests.env_files import use_env_files
from base.paths.home import resolve_meiva_home

_NO_DATA_PLANE_IDENTITY_LINES = (
    f"MEIVA_CLUSTER_SECRET={os.environ['MEIVA_CLUSTER_SECRET']}",
    f"MEIVA_GATEWAY_URL={os.environ['MEIVA_GATEWAY_URL']}",
)


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """`enforce_cluster_env_authority` and the CLI entry mutate os.environ directly,
    not through monkeypatch: put the whole environment back after each test."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _point_env_at_without_data_plane_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An env file that declares neither data-plane URL — the pure agent-runner
    shape for MEIVA_DB_URL and MEIVA_REDIS_URL together (#4334)."""
    env_file = tmp_path / "no-data-plane.env"
    env_file.write_text(
        "MEIVA_AGENT_HOST_HEALTH_PORT=18035\n" + "\n".join(_NO_DATA_PLANE_IDENTITY_LINES) + "\n"
    )
    use_env_files(monkeypatch, env_file)


def test_cli_entry_records_launcher_profile_and_keeps_undeclared_projections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full CLI shape (#4334): the entry helper pops the live marker and
    records it, and the later authority pass — reading the recorded context —
    keeps both undeclared launcher projections. This is the health-probe flow
    that was red: agent child -> CLI pop -> drop -> sentinel (env-class)."""
    from cli.main import normalize_process_profile

    monkeypatch.delitem(os.environ, "MEIVA_PROCESS_PROFILE", raising=False)
    # The entry helper records the launcher profile next. Register the absent key (setitem)
    # before removing it: `delitem(raising=False)` on an absent key records nothing, so the
    # recorded value would outlive the test.
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "")
    monkeypatch.delitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY)
    monkeypatch.setitem(os.environ, "MEIVA_PROCESS_PROFILE", "agent")
    _point_env_at_without_data_plane_urls(monkeypatch, tmp_path)
    monkeypatch.setitem(
        os.environ,
        "MEIVA_DB_URL",
        "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva",
    )
    monkeypatch.setitem(
        os.environ, "MEIVA_REDIS_URL", "redis://meiva:runtime-password@127.0.0.1:6380/0"
    )
    # Companion: inherited keys the `.env` does not declare still drop in the
    # same pass — the exemption must not spill.
    monkeypatch.setitem(os.environ, "MEIVA_APP_PORT", "3001")

    normalize_process_profile()

    assert "MEIVA_PROCESS_PROFILE" not in os.environ
    assert os.environ[dotenv_boot.LAUNCHER_PROFILE_ENV_KEY] == "agent"

    dotenv_boot.enforce_cluster_env_authority(resolve_meiva_home())

    assert (
        os.environ["MEIVA_DB_URL"]
        == "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"
    )
    assert os.environ["MEIVA_REDIS_URL"] == "redis://meiva:runtime-password@127.0.0.1:6380/0"
    assert "MEIVA_APP_PORT" not in os.environ
