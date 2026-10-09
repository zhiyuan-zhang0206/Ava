"""MCP dispatch respects external attachment admission and fallback revalidation."""

from __future__ import annotations

from typing import Any

import pytest

from tests.factories.external_attachment import attached_mcp_runtime as attached_mcp_runtime
from tests.factories.external_attachment import attached_runtime as attached_runtime


def test_expired_lease_cannot_dispatch_through_local_mcp(
    attached_mcp_runtime: tuple[dict[str, Any], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import mcps

    lease, attachment = attached_mcp_runtime
    dispatched: list[bool] = []

    def local_dispatch(coroutine: Any) -> dict[str, Any]:
        coroutine.close()
        dispatched.append(True)
        return {}

    monkeypatch.setattr(mcps, "_get_remote_client", lambda: None)
    monkeypatch.setattr(mcps, "_run_async", local_dispatch)
    lease["status"] = "expired"
    try:
        with pytest.raises(RuntimeError, match="expired"):
            mcps._call_raw("example", "side_effect")
        assert not dispatched
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()


def test_mcp_revalidates_lease_before_transport_fallback(
    attached_mcp_runtime: tuple[dict[str, Any], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import mcps

    lease, attachment = attached_mcp_runtime

    class FailedDaemon:
        def call_tool(self, *_args: Any, timeout_seconds: float) -> dict[str, Any]:
            lease["status"] = "expired"
            raise mcps.MCPConnectError("daemon disconnected")

    monkeypatch.setattr(mcps, "_get_remote_client", FailedDaemon)
    try:
        with pytest.raises(RuntimeError, match="expired"):
            mcps._call_raw("example", "side_effect")
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()
