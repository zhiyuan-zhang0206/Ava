from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ava
from agent.tests.impersonation.external_attachment import (
    attached_mcp_runtime as attached_mcp_runtime,
)
from agent.tests.impersonation.external_attachment import attached_runtime as attached_runtime


def test_expired_lease_cannot_dispatch_through_ava_mcps(
    attached_mcp_runtime: tuple[dict[str, Any], Any], unit_home: Path
) -> None:
    lease, attachment = attached_mcp_runtime
    (unit_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"example": {"command": "true"}}}), encoding="utf-8"
    )
    lease["status"] = "expired"
    try:
        with pytest.raises(RuntimeError, match="expired"):
            ava.mcps.example.raw("side_effect")
    finally:
        with pytest.raises(RuntimeError, match="expired"):
            attachment.close()
