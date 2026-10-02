"""The cluster resume checklist names only commands that parse."""

from __future__ import annotations

import pytest

import cli.commands.cluster.control as cluster_control
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResponse
from tests.cli._commands_helpers import _gateway_role_pinned as _gateway_role_pinned
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base


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

    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("base.cluster.machine.gateway_auth_headers", dict)
    monkeypatch.setattr(
        "base.host.net.http_dial.post",
        lambda *_a, **_kw: _FakeResponse({"name": "wsl", "resumed": True}),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert cluster_control.cmd_cluster_resume("wsl") == 0
    out = capsys.readouterr().out
    commands = re.findall(r"`(ava [^`]+)`", out)
    assert commands, out
    parser = build_parser()
    for command in commands:
        parser.parse_args(command.split()[1:])  # SystemExit(2) fails the test
    assert "`ava restart`" in out
    assert "--restart-only" not in out
