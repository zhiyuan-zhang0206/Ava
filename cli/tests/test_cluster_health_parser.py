"""The `ava cluster health` argument surface."""

import pytest

from cli.commands.cluster.tests.test_cluster_health import (
    _all_checks_pass as _all_checks_pass,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _home as _home,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _no_deploy_in_flight as _no_deploy_in_flight,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _provider_guard_healthy as _provider_guard_healthy,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _sent_alerts as _sent_alerts,
)


@pytest.mark.parametrize(
    "arguments",
    [
        ["health-probe", "--auto-rollback"],
        ["health-probe", "--threshold", "3"],
        ["health-probe-register", "--threshold", "3"],
    ],
)
def test_parser_rejects_removed_release_policy_flags(arguments: list[str]) -> None:
    from cli.main import _build_parser

    with pytest.raises(SystemExit) as refused:
        _build_parser().parse_args(["cluster", *arguments])
    assert refused.value.code == 2


def test_health_probe_dispatch_preserves_observation_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.commands.cluster.health as _cluster_health_commands
    from cli.main import _build_parser

    received: list[dict[str, object]] = []

    def probe(**kwargs: object) -> int:
        received.append(kwargs)
        return 1

    monkeypatch.setattr(_cluster_health_commands, "cmd_health_probe", probe)
    args = _build_parser().parse_args(
        ["cluster", "health-probe", "--agent-min", "2", "--crash-loop-max-restarts", "4"]
    )
    assert args.func(args) == 1
    assert received == [
        {
            "agent_min": 2,
            "crash_loop_max_restarts": 4,
            "crash_loop_window_minutes": 10,
            "check_crash_loops": True,
            "check_schema": True,
        }
    ]


@pytest.mark.parametrize(
    ("flag", "destination"),
    [
        ("crash-loop-check", "crash_loop_check"),
        ("schema-check", "schema_check"),
    ],
)
def test_health_probe_check_flags_disable_only_when_negated(flag: str, destination: str) -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    default = parser.parse_args(["cluster", "health-probe"])
    disabled = parser.parse_args(["cluster", "health-probe", f"--no-{flag}"])
    assert getattr(default, destination) is True
    assert getattr(disabled, destination) is False
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["cluster", "health-probe", f"--{flag}"])
    assert refused.value.code == 2
