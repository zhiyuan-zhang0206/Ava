from pathlib import Path

import pytest

from base import paths


@pytest.mark.parametrize("home_name", ["first", "second"])
def test_runtime_path_uses_the_explicit_home_without_creating_it(
    tmp_path: Path, home_name: str
) -> None:
    home = tmp_path / home_name
    derived = [
        (paths.run_dir(home), "run"),
        (paths.exec_run_dir(home), "run/exec"),
        (paths.root_run_dir(home), "run/meiva-root"),
        (paths.root_manifests_path(home), "run/meiva-root/manifests.json"),
        (paths.quarantined_exec_requests_dir(home), "quarantined-exec-requests"),
        (paths.computer_mcp_socket(home), "run/computer-mcp.sock"),
        (paths.insights_socket(home), "run/insights.sock"),
        (paths.insights_pidfile(home), "run/insights.pid"),
        (paths.mcp_daemon_shared_socket(home), "run/mcp_daemon.sock"),
        (paths.chrome_mcp_socket(home, 9222), "run/chrome-mcp.9222.sock"),
        (paths.permissions_helper_socket(home, 8765), "run/permissions-helper.8765.sock"),
        (paths.otel_collector_dir(home), "otel-collector"),
        (paths.otel_collector_binary(home), "otel-collector/otelcol-contrib"),
        (paths.otel_collector_config(home), "otel-collector/config.yaml"),
    ]

    for actual, relative in derived:
        assert Path(actual) == home / relative
    assert list(tmp_path.iterdir()) == []
