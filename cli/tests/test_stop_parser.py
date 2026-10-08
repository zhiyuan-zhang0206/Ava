"""`ava stop` parses its flags, `ava pause` no longer exists, and a plain start needs no manual operation."""

from __future__ import annotations

import pytest

from base.deploy.maintenance import admission
from cli.commands.lifecycle._pause_resume import resume_after_start
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.parsers import build_parser
from tests.components.agent.test_maintenance import isolate as isolate


def test_plain_start_and_parser_need_no_manual_operation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @resume_after_start
    def start() -> int:
        assert not admission.held()
        return 0

    assert start() == start() == 0
    assert "hold released" not in capsys.readouterr().out
    parser = build_parser()
    stop = parser.parse_args(["stop", "--keep-infra", "--keep-service", "gateway", "--force"])
    assert stop.keep_infra and stop.keep_service == ["gateway"] and stop.force
    kept = parser.parse_args(["stop", "-y", "--keep-infra", "--keep-service", "frontend"])
    assert kept.keep_service == ["frontend"] and not kept.force and kept.timeout == 300


def test_pause_is_not_a_verb(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pause"])
    assert "invalid choice: 'pause'" in capsys.readouterr().err


def test_removed_stop_and_restart_flags_are_refused() -> None:
    """The zero-effect `stop --stop-browser` and `restart --force-reap` no-ops are gone."""
    parser = build_parser()
    for argv in (["stop", "--stop-browser"], ["restart", "--force-reap"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
