"""CLI launcher context (#4334): the entry pop records, the gates read it.

Split out of test_dotenv_boot.py to stay under the structure-lint's per-file
line budget.

cli.main's entry pops AVA_PROCESS_PROFILE (settings-full by design) and
records the popped value as AVA_LAUNCHER_PROFILE; the authority pass reads
the live-or-recorded context (`_launcher_context`). With only the live
marker consulted, both launcher-projection exemptions were unreachable on
every CLI path: `ava cluster health-probe` run from an agent child on a pure
agent-runner dropped the launcher's runner URLs and fell back to the
sentinel (env-class red).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared import dotenv_boot
from tests.shared.test_dotenv_boot import (
    _IDENTITY_LINES,
    _point_env_at_without_db_url,
    _restore_authority_env,  # noqa: F401 — shared fixture  # pyright: ignore[reportUnusedImport] — pytest fixture import
)

_NO_DATA_PLANE_IDENTITY_LINES = tuple(
    ln for ln in _IDENTITY_LINES if ln.startswith(("AVA_CLUSTER_SECRET=", "AVA_GATEWAY_URL="))
)


def _point_env_at_without_data_plane_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An env file that declares neither data-plane URL — the pure agent-runner
    shape for AVA_DB_URL and AVA_REDIS_URL together (#4334)."""
    env_file = tmp_path / "no-data-plane.env"
    env_file.write_text(
        "AVA_AGENT_HOST_HEALTH_PORT=18035\n" + "\n".join(_NO_DATA_PLANE_IDENTITY_LINES) + "\n"
    )
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "absent-mirror.env")


def test_cli_entry_records_launcher_profile_and_keeps_undeclared_projections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full CLI shape (#4334): the entry helper pops the live marker and
    records it, and the later authority pass — reading the recorded context —
    keeps both undeclared launcher projections. This is the health-probe flow
    that was red: agent child -> CLI pop -> drop -> sentinel (env-class)."""
    from cli.main import _normalize_process_profile

    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, raising=False)
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

    dotenv_boot._enforce_cluster_env_authority()

    assert os.environ["AVA_DB_URL"] == "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"
    assert os.environ["AVA_REDIS_URL"] == "redis://ava:runtime-password@127.0.0.1:6380/0"
    assert "AVA_APP_PORT" not in os.environ


def test_recorded_launcher_profile_drops_undeclared_owner_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorded context keeps the live one's runner-shape gate: an
    inherited OWNER url on an undeclared unit still drops — recording the
    profile must not widen the exemption."""
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "agent")
    _point_env_at_without_db_url(monkeypatch, tmp_path)
    monkeypatch.setitem(
        os.environ, "AVA_DB_URL", "postgresql://ava_main:owner-password@127.0.0.1:5433/ava"
    )

    dotenv_boot._enforce_cluster_env_authority()

    assert "AVA_DB_URL" not in os.environ


def test_recorded_launcher_profile_drops_malformed_db_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The parse gate holds for the recorded context too: `urlsplit` itself
    rejects the value (invalid IPv6), so it drops instead of raising."""
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "agent")
    _point_env_at_without_db_url(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://[::1")

    dotenv_boot._enforce_cluster_env_authority()

    assert "AVA_DB_URL" not in os.environ


@pytest.mark.parametrize("value", ["redis://[::1", "redis://"])
def test_recorded_launcher_profile_drops_unparseable_redis_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    """The redis exemption's parse gate: a `urlsplit` failure (invalid IPv6)
    and a parse with no host both drop — never raise, never keep a value that
    cannot dial."""
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "agent")
    _point_env_at_without_data_plane_urls(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", value)

    dotenv_boot._enforce_cluster_env_authority()

    assert "AVA_REDIS_URL" not in os.environ


def test_unanchored_checkout_drops_recorded_launcher_projections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The anchored gate holds for the recorded context too: a bare worktree's
    CLI keeps the sentinel discipline — the record must not let an agent
    shell's inherited URLs smuggle the host home's data plane into a worktree
    that never claimed it."""
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "agent")
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    _point_env_at_without_data_plane_urls(monkeypatch, tmp_path)
    monkeypatch.setitem(
        os.environ, "AVA_DB_URL", "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"
    )
    monkeypatch.setitem(
        os.environ, "AVA_REDIS_URL", "redis://ava:runtime-password@127.0.0.1:6380/0"
    )

    dotenv_boot._enforce_cluster_env_authority()

    assert "AVA_DB_URL" not in os.environ
    assert "AVA_REDIS_URL" not in os.environ


@pytest.mark.parametrize("context", ["live", "recorded"])
def test_launcher_context_keeps_undeclared_redis_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: str
) -> None:
    """Both context forms keep a parseable undeclared redis URL — the mirror
    of the DB exemption (#4334): the live marker for agent/daemon processes,
    the recorded one for CLI paths. No username shape gates it (the URL
    carries the cluster's runtime ACL identity, the same one the gateway's own
    URL does)."""
    monkeypatch.delitem(os.environ, "AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, raising=False)
    if context == "live":
        monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    else:
        monkeypatch.setitem(os.environ, dotenv_boot.LAUNCHER_PROFILE_ENV_KEY, "agent")
    _point_env_at_without_data_plane_urls(monkeypatch, tmp_path)
    monkeypatch.setitem(
        os.environ, "AVA_REDIS_URL", "redis://ava:runtime-password@127.0.0.1:6380/0"
    )

    dotenv_boot._enforce_cluster_env_authority()

    assert os.environ["AVA_REDIS_URL"] == "redis://ava:runtime-password@127.0.0.1:6380/0"
