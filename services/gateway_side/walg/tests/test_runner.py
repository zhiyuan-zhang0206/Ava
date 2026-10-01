"""Every wal-g invocation: fixed argv shape, a daemon-grade environment, bounded time."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.gateway_side.walg import runner
from services.gateway_side.walg.config import WalgConfigError
from services.gateway_side.walg.tests.support import Sandbox, make_sandbox


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def test_the_environment_carries_process_mechanics_and_no_ava_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVA_API_TOKEN", "token-value")
    monkeypatch.setenv("AVA_TEST_ONLY_SECRET", "secret-value")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "ambient-secret")

    env = runner.walg_env()

    assert "PATH" in env
    assert not [name for name in env if name.startswith("AVA_")]
    assert "OSS_ACCESS_KEY_SECRET" not in env, "storage credentials come from the 0600 file only"


def test_postgres_connection_variables_name_the_owner_only_socket() -> None:
    env = runner.walg_env(
        pg_admin_url="postgresql://zzy@/postgres?host=/sockets/ava-pg-home&port=5433",
        extra={"WALG_DELTA_MAX_STEPS": "6"},
    )

    assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGDATABASE"]) == (
        "/sockets/ava-pg-home",
        "5433",
        "zzy",
        "postgres",
    )
    assert env["WALG_DELTA_MAX_STEPS"] == "6"
    assert "PGPASSWORD" not in env


def test_the_call_is_wal_g_with_the_config_file_and_the_given_arguments(sandbox: Sandbox) -> None:
    runner.run_walg(["st", "ls"], timeout_s=30)

    assert sandbox.calls() == ["st ls"]


def test_json_output_is_parsed(sandbox: Sandbox) -> None:
    assert runner.run_walg(["backup-list", "--json"], timeout_s=30, as_json=True) == [
        {"backup_name": "base_000000010000000000000002"}
    ]


def test_a_failing_call_reports_the_exit_code_and_stderr_tail(sandbox: Sandbox) -> None:
    sandbox.set_mode("fail")

    with pytest.raises(
        runner.WalgCommandError, match=r"wal-g st failed \(exit 1\).*simulated failure"
    ):
        runner.run_walg(["st", "ls"], timeout_s=30)


def test_a_hung_call_is_cut_off(sandbox: Sandbox) -> None:
    sandbox.set_mode("hang")

    with pytest.raises(runner.WalgCommandError, match="did not finish within 1s"):
        runner.run_walg(["st", "ls"], timeout_s=1)
    sandbox.set_mode("ok")


def test_off_means_no_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)

    with pytest.raises(WalgConfigError, match="AVA_WALG_CONFIG_FILE is not set"):
        runner.run_walg(["st", "ls"], timeout_s=30)

    assert sandbox.calls() == []


def test_the_logged_variant_returns_both_streams(sandbox: Sandbox) -> None:
    sandbox.put(
        "delete-dry.log", "INFO: Object marked for deletion: wal_005/X.lz4 storage=default\n"
    )

    output = runner.run_walg_logged(["delete", "retain", "FULL", "3"], timeout_s=30)

    assert output.stdout == ""
    assert "Object marked for deletion: wal_005/X.lz4" in output.stderr
