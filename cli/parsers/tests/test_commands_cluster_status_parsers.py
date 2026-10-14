"""The cluster resume checklist names only commands that parse."""

from __future__ import annotations

import pytest

import cli.commands.cluster.control as cluster_control
from base.cluster import machine
from base.host.net import http_dial
from cli.database.tests.command_fakes import FakeResponse
from cli.database.tests.command_fakes import fake_session_backends as fake_session_backends
from cli.database.tests.command_fakes import gateway_role_pinned as gateway_role_pinned
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_cmd_cluster_resume_checklist_names_only_commands_that_parse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every `ava ...` command the resume checklist prints must parse with the real
    CLI parser — an operator following it after an address change is exactly the
    person who cannot afford a step that exits 2. The pg_hba step names the
    current regeneration path: a gateway `ava restart` rewrites pg_hba.conf and
    reloads the retained Postgres on its start leg."""
    import re

    from cli.parsers import build_parser

    monkeypatch.setattr(machine, "gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(machine, "gateway_auth_headers", dict)
    monkeypatch.setattr(
        http_dial,
        "post",
        lambda *_a, **_kw: FakeResponse({"name": "runner-a", "resumed": True}),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert cluster_control.cmd_cluster_resume("runner-a") == 0
    out = capsys.readouterr().out
    commands = re.findall(r"`(ava [^`]+)`", out)
    assert commands, out
    parser = build_parser()
    for command in commands:
        parser.parse_args(command.split()[1:])  # SystemExit(2) fails the test
    assert "`ava restart`" in out
    assert "--restart-only" not in out
