"""The `ava cluster health` argument surface."""

import pytest

from cli.commands.cluster.tests.test_cluster_health import (
    _all_checks_pass as _all_checks_pass,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _home as _home,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _provider_guard_healthy as _provider_guard_healthy,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _ran as _ran,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _signals as _signals,
)


def test_health_probe_dispatch_preserves_observation_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.commands.cluster.health as _cluster_health_commands
    from cli.parsers import parse_args

    received: list[dict[str, object]] = []

    def probe(**kwargs: object) -> int:
        received.append(kwargs)
        return 1

    monkeypatch.setattr(_cluster_health_commands, "cmd_health_probe", probe)
    args = parse_args(["cluster", "health-probe", "--no-schema-check"])
    assert args.func(args) == 1
    assert received == [
        {
            "check_schema": False,
        }
    ]


@pytest.mark.parametrize(
    ("flag", "destination"),
    [
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
