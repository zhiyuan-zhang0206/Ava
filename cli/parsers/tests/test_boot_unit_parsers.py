"""The CLI parser exposes no second Linux boot-unit verb surface."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("verb", ["install", "uninstall", "status"])
def test_no_second_linux_boot_management_cli(verb: str) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["cluster", "boot-unit", verb])
    assert error.value.code == 2
