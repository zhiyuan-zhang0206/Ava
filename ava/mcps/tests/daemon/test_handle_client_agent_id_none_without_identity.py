"""Daemon cases: handle client agent id none without identity."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps._daemon as daemon_mod
from ava.mcps.tests.test_daemon import (
    _call_result,
    _content,
    _FakeWriter,
    _make_reader,
    _make_session,
    _write_config,
    _writer_arg,
)
from ava.mcps.tests.test_daemon import (
    _no_reap_stale_daemons as _no_reap_stale_daemons,
)
from ava.mcps.tests.test_daemon import (
    daemon_wide as daemon_wide,
)
from ava.mcps.tests.test_daemon import (
    fake_home as fake_home,
)
from ava.mcps.tests.test_daemon import (
    scope as scope,
)
from ava.mcps.tests.test_daemon import (
    short_socket_path as short_socket_path,
)


async def test_handle_client_agent_id_none_without_identity(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """A request without an agent_id stamps None — the service falls back to
    per-connection affinity for identity-less clients."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result([_content({"type": "text", "text": "hi"})]),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {}},
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    assert session.client_agent_id is None
