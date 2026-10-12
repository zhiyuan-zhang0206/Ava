"""MCP dispatch respects external attachment admission and fallback revalidation."""

from __future__ import annotations

from typing import Any

import pytest

from tests.factories.external_attachment import attached_mcp_runtime as attached_mcp_runtime
from tests.factories.external_attachment import attached_runtime as attached_runtime


def test_expired_lease_cannot_dispatch_through_daemon(
    attached_mcp_runtime: tuple[dict[str, Any], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from ava import mcps

    lease, attachment = attached_mcp_runtime
    remote = MagicMock()
    monkeypatch.setattr(mcps, "_get_remote_client", lambda: remote)
    lease["status"] = "expired"
    try:
        with pytest.raises(RuntimeError, match="expired"):
            mcps._call_raw("example", "side_effect")
        remote.call_tool.assert_not_called()
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()
