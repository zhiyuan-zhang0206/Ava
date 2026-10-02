"""What a completed stop says when Postgres' shutdown had to be escalated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.deploy.lifecycle import status_journal
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle.tests.stop_support import home as home

NOTE = "postgres fast shutdown did not complete within 277s; killed leftover processes: 4242"


def test_an_escalated_shutdown_is_in_the_report_and_the_journal(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status_journal.begin("stop")

    assert command._finish_stop(owns_journal=True, notes=[NOTE]) == 0

    assert f"Stop completed with an escalation: {NOTE}" in capsys.readouterr().err
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == 0
    assert journal["result"]["escalations"] == [NOTE]


def test_a_stop_without_an_escalation_says_nothing_extra(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status_journal.begin("stop")

    assert command._finish_stop(owns_journal=True, notes=[]) == 0

    assert capsys.readouterr().err == ""
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert "escalations" not in journal["result"]
