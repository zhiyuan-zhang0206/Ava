"""Daemon cases: handle client agent id none without identity."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps._daemon as daemon_mod
from ava.mcps.tests.test_daemon import (
    FakeWriter,
    call_result,
    content,
    make_reader,
    make_session,
    write_config,
    writer_arg,
)
from ava.mcps.tests.test_daemon import (
    daemon_wide as daemon_wide,
)
from ava.mcps.tests.test_daemon import (
    fake_home as fake_home,
)
from ava.mcps.tests.test_daemon import (
    no_reap_stale_daemons as no_reap_stale_daemons,
)
from ava.mcps.tests.test_daemon import (
    scope as scope,
)
from ava.mcps.tests.test_daemon import (
    short_socket_path as short_socket_path,
)


async def test_handle_client_agent_id_none_without_identity(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod.Scope
) -> None:
    """A request without an agent_id stamps None — the service falls back to
    per-connection affinity for identity-less clients."""
    write_config(fake_home, {"fs": {"command": "x"}})
    session = make_session(
        call_result=call_result([content({"type": "text", "text": "hi"})]),
    )
    monkeypatch.setattr(
        daemon_mod, "connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {}},
    }
    reader = make_reader([(json.dumps(req) + "\n").encode()])
    writer = FakeWriter()
    await daemon_mod.handle_client(
        reader,
        writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    assert session.client_agent_id is None
