"""The commands that operator hints name are commands the parser accepts.

`cli/commands/tests/test_commands_register.py` pins which commands the schema and
registration hints name; this pins that each one parses, without the commands tests
importing the parser above them.
"""

from __future__ import annotations

import pytest

from cli.parsers import build_parser


@pytest.mark.parametrize("command", ["stop", "start"])
def test_hint_commands_parse(command: str) -> None:
    build_parser().parse_args([command])  # SystemExit(2) fails the test
