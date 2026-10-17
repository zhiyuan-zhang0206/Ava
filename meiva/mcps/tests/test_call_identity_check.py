from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from meiva.mcps._tools import call_raw
from meiva.mcps.tests._mcps_helpers import tool_source


def test_a_failed_identity_check_stops_the_call_before_the_daemon() -> None:
    remote = MagicMock()

    def expired() -> None:
        raise RuntimeError("external lease expired")

    source = tool_source(lambda: remote, validate_identity=expired)

    with pytest.raises(RuntimeError, match="expired"):
        call_raw(source, "example", "side_effect")

    remote.call_tool.assert_not_called()


def test_a_passed_identity_check_dispatches_the_call() -> None:
    remote = MagicMock()
    remote.call_tool.return_value = {"content": [], "isError": False, "structuredContent": None}
    checks: list[str] = []
    source = tool_source(lambda: remote, validate_identity=lambda: checks.append("checked"))

    call_raw(source, "example", "side_effect", a=1)

    assert checks == ["checked"]
    remote.call_tool.assert_called_once()
