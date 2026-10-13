"""MCP dispatch respects external attachment admission and fallback revalidation."""

from __future__ import annotations

from typing import Any

import pytest

from tests.factories.external_attachment import attached_mcp_runtime as attached_mcp_runtime
from tests.factories.external_attachment import attached_runtime as attached_runtime


def test_expired_lease_cannot_dispatch_through_daemon(
    attached_mcp_runtime: tuple[dict[str, Any], Any],
) -> None:
    from unittest.mock import MagicMock

    from ava.mcps._tools import ToolSource, call_raw
    from ava.mcps.tests._mcps_helpers import NoCache
    from ava.sdk_surface import agent_identity

    lease, attachment = attached_mcp_runtime
    remote = MagicMock()
    source = ToolSource(
        remote=lambda: remote,
        timeout_seconds=lambda: 5.0,
        cache=NoCache(),
        validate_identity=agent_identity.validate_external_identity,
    )
    lease["status"] = "expired"
    try:
        with pytest.raises(RuntimeError, match="expired"):
            call_raw(source, "example", "side_effect")
        remote.call_tool.assert_not_called()
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()
