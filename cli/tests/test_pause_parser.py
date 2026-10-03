"""`ava pause` / `ava stop` parse their flags, and a plain start needs no manual operation."""

from __future__ import annotations

import pytest

from base.deploy.maintenance import admission
from cli.commands.lifecycle._pause_resume import resume_after_start
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.parsers import build_parser
from tests.agent.test_maintenance import isolate as isolate


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
    pause = parser.parse_args(["pause", "--keep-service", "frontend"])
    stop = parser.parse_args(["stop", "--keep-infra", "--keep-service", "gateway", "--force"])
    assert pause.keep_service == ["frontend"] and not pause.force
    assert stop.keep_infra and stop.force and stop.stop_browser
