from __future__ import annotations

import pytest

from cli.parsers import build_parser


@pytest.mark.parametrize("command", ["stop", "start"])
def test_hint_commands_parse(command: str) -> None:
    build_parser().parse_args([command])
